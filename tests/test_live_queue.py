"""The one live queue (karaoke.live_queue) against the test Postgres DB."""
import threading
from unittest.mock import patch

import pytest

from karaoke import collection, live_queue, localcache


def _row(tid):
    return {"track_id": tid, "artist": f"A{tid}", "title": f"T{tid}",
            "url": f"https://music.youtube.com/watch?v=v{tid}", "kind": "youtube_music",
            "bpm": None, "key": None, "energy": None}


@pytest.fixture
def fake_library():
    def rows_for(ids, conn):
        return [_row(int(t)) for t in ids if int(t) < 1000]
    with patch.object(collection, "rows_for", side_effect=rows_for):
        yield


def _ids():
    q = localcache.load_active_queue()
    return [r["track_id"] for r in q["rows"]], q["current_index"]


def _seed(ids, cur):
    localcache.save_active_queue("p1", "Hip Hop", [_row(i) for i in ids], current_index=cur)


def test_enqueue_creates_queue_and_appends(fake_library):
    with localcache.connect() as conn:
        out = live_queue.enqueue([1, 2], conn, position="end")
        assert out["created"] is True and out["added_count"] == 2
        live_queue.enqueue([3], conn, position="end")
    assert _ids() == ([1, 2, 3], 0)


def test_enqueue_next_inserts_after_current_in_order(fake_library):
    _seed([1, 2, 3], 1)
    with localcache.connect() as conn:
        out = live_queue.enqueue([7, 8], conn, position="next")
    assert _ids() == ([1, 2, 7, 8, 3], 1)
    assert [t["position"] for t in out["tracks"]] == [3, 4]


def test_enqueue_next_moves_an_already_upcoming_song_up(fake_library):
    _seed([1, 2, 3, 4], 0)
    with localcache.connect() as conn:
        out = live_queue.enqueue([4], conn, position="next")
    assert _ids() == ([1, 4, 2, 3], 0)
    assert out["moved_count"] == 1 and out["added_count"] == 0


def test_enqueue_skips_current_and_upcoming_duplicates(fake_library):
    _seed([1, 2, 3], 1)
    with localcache.connect() as conn:
        out = live_queue.enqueue([2, 3], conn, position="end")
    assert out["duplicate"] is True
    assert _ids() == ([1, 2, 3], 1)


def test_enqueue_after_exhausted_queue_plays_next(fake_library):
    _seed([1, 2], 2)   # follower reached the end
    with localcache.connect() as conn:
        live_queue.enqueue([5], conn, position="next")
    assert _ids() == ([1, 2, 5], 2)


def test_enqueue_unknown_track_reports_error(fake_library):
    with localcache.connect() as conn:
        out = live_queue.enqueue([5000], conn)
    assert out["ok"] is False


def test_clear_upcoming_keeps_playing_song():
    _seed([1, 2, 3, 4], 1)
    with localcache.connect() as conn:
        out = live_queue.clear_upcoming(conn)
    assert out["removed"] == 2
    assert _ids() == ([1, 2], 1)


def test_clear_upcoming_from_off_queue_start_drops_unplayed_current():
    # Stage anchor: playing song not in the queue -> upcoming starts AT current_index.
    _seed([1, 2, 3], 1)
    with localcache.connect() as conn:
        live_queue.clear_upcoming(conn, from_index=1)
    assert _ids() == ([1], 1)


def test_clear_never_reaches_into_history():
    _seed([1, 2, 3], 2)
    with localcache.connect() as conn:
        live_queue.clear_upcoming(conn, from_index=0)
    assert _ids() == ([1, 2], 2)


def test_remove_uses_track_id_when_index_went_stale():
    _seed([1, 2, 3, 4], 0)
    with localcache.connect() as conn:
        # UI rendered 4 at index 3, but something inserted a row since.
        localcache.save_active_queue("p1", "Hip Hop", [_row(i) for i in [1, 9, 2, 3, 4]])
        out = live_queue.remove(3, conn, track_id=4)
    assert out["ok"] and out["title"] == "T4"
    assert _ids() == ([1, 9, 2, 3], 0)


def test_remove_missing_song_is_an_error():
    _seed([1, 2], 0)
    with localcache.connect() as conn:
        out = live_queue.remove(1, conn, track_id=99)
    assert out["ok"] is False
    assert _ids() == ([1, 2], 0)


def test_remove_history_row_keeps_current_song_current():
    _seed([1, 2, 3], 2)
    with localcache.connect() as conn:
        live_queue.remove(0, conn, track_id=1)
    assert _ids() == ([2, 3], 1)


def test_play_next_and_move_stay_out_of_history():
    _seed([1, 2, 3, 4, 5], 1)
    with localcache.connect() as conn:
        live_queue.play_next(4, conn, track_id=5)
        assert _ids() == ([1, 2, 5, 3, 4], 1)
        live_queue.move(4, 0, conn, track_id=4)   # clamped to current_index
        assert _ids() == ([1, 4, 2, 5, 3], 1)
        out = live_queue.move(0, 3, conn, track_id=1)  # history row: refused
    assert out["ok"] is False


def test_shuffle_only_touches_upcoming():
    import random
    _seed([1, 2, 3, 4, 5, 6], 1)
    with localcache.connect() as conn:
        live_queue.shuffle_upcoming(conn, rng=random.Random(3))
    ids, cur = _ids()
    assert ids[:2] == [1, 2] and sorted(ids[2:]) == [3, 4, 5, 6] and cur == 1


def test_set_index_does_not_drop_concurrently_queued_rows(fake_library):
    """The follower's advance used to re-save its stale row list."""
    _seed([1, 2], 0)
    stale = localcache.load_active_queue()
    with localcache.connect() as conn:
        live_queue.enqueue([3], conn, position="end")
    from karaoke import queue_follower
    queue_follower._save_index(stale, 1)
    assert _ids() == ([1, 2, 3], 1)


def test_concurrent_enqueues_are_not_lost(fake_library):
    _seed([1], 0)
    errors = []

    def add(tid):
        try:
            with localcache.connect() as conn:
                live_queue.enqueue([tid], conn, position="end")
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(t,)) for t in range(10, 30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    ids, _ = _ids()
    assert sorted(ids) == [1] + list(range(10, 30))


def test_upcoming_start_anchors_on_playing_song():
    rows = [_row(1), _row(2), _row(3)]
    assert live_queue.upcoming_start(rows, 0, track_id=2) == 2
    assert live_queue.upcoming_start(rows, 0, artist="a3", title="t3") == 3
    assert live_queue.upcoming_start(rows, 1, track_id=99) == 1   # off-queue play


def test_mutate_without_change_does_not_bump_updated_at():
    _seed([1, 2], 0)
    before = localcache.active_queue_updated_at()
    localcache.mutate_active_queue(lambda state: None)
    assert localcache.active_queue_updated_at() == before


# --- dashboard endpoints + AI DJ tool hit the same queue ----------------------

def test_dashboard_queue_endpoints_round_trip(fake_library):
    from fastapi.testclient import TestClient
    from karaoke import dashboard_api

    client = TestClient(dashboard_api.app)
    _seed([1, 2, 3, 4, 5], 1)

    snap = client.get("/api/queue").json()
    assert snap["current_index"] == 1 and snap["upcoming_count"] == 3
    assert snap["rows"][4] == {**snap["rows"][4], "index": 4, "track_id": 5}

    r = client.post("/api/queue/move", json={"index": 4, "track_id": 5, "to_index": 2})
    assert r.status_code == 200 and _ids() == ([1, 2, 5, 3, 4], 1)

    r = client.post("/api/queue/remove", json={"index": 3, "track_id": 3})
    assert r.status_code == 200 and _ids() == ([1, 2, 5, 4], 1)

    r = client.post("/api/queue/remove", json={"index": 3, "track_id": 77})
    assert r.status_code == 409

    r = client.post("/api/queue/add", json={"track_ids": [8], "position": "next"})
    assert r.json()["status"] == "queued" and _ids() == ([1, 2, 8, 5, 4], 1)

    r = client.post("/api/queue/clear", json={"from_index": 2})
    assert r.json()["removed"] == 3 and _ids() == ([1, 2], 1)


def test_ai_dj_tools_edit_the_same_queue(fake_library):
    import json
    from karaoke import mcp_server

    _seed([1, 2, 3], 0)
    json.loads(mcp_server.queue_next_in_player(track_ids=[9], position="end"))
    live = json.loads(mcp_server.get_live_queue())
    assert [t["track_id"] for t in live["upcoming_tracks"]] == [1, 2, 3, 9]
    assert live["upcoming_tracks"][0]["is_current"] is True

    out = json.loads(mcp_server.edit_live_queue("remove", index=2, track_id=3))
    assert out["ok"] and _ids() == ([1, 2, 9], 0)
    out = json.loads(mcp_server.edit_live_queue("clear"))
    assert out["removed"] == 2 and _ids() == ([1], 0)
    assert json.loads(mcp_server.edit_live_queue("bogus"))["ok"] is False


# --- radio keep-going (5x5 rolling window on new radio detection) -------------

class _FakeSuggestion:
    def __init__(self, track_id, artist=None, title=None):
        self.track_id = track_id
        self.artist = artist or f"A{track_id}"
        self.title = title or f"T{track_id}"


def test_radio_keep_going_5x5_batches_and_rotation(fake_library):
    """5 detections grow 5->10->15->20->25; 6th & 7th rotate the oldest 5 out, no doubles."""
    from karaoke import queue_suggest

    localcache.set_queue_autopilot(True)
    live_queue._RADIO_EVICTED_IDS.clear()
    live_queue._RADIO_EVICTED_PAIRS.clear()

    # Map each seed song to candidate IDs (including overlaps and the seed itself
    # to confirm deduplication).
    seed_map = {
        ("RadioArtist1", "RadioTitle1"): 101,
        ("RadioArtist2", "RadioTitle2"): 102,
        ("RadioArtist3", "RadioTitle3"): 103,
        ("RadioArtist4", "RadioTitle4"): 104,
        ("RadioArtist5", "RadioTitle5"): 105,
        ("RadioArtist6", "RadioTitle6"): 106,
        ("RadioArtist7", "RadioTitle7"): 107,
    }
    pool_by_seed = {
        101: [101, 1, 2, 3, 4, 5, 6],
        102: [1, 2, 6, 7, 8, 9, 10, 11],
        103: [5, 10, 11, 12, 13, 14, 15, 16],
        104: [16, 17, 18, 19, 20, 21],
        105: [21, 22, 23, 24, 25, 26],
        # 6th batch includes 1..5 (about to be rotated out) to verify they are NOT re-added
        106: [1, 2, 3, 26, 27, 28, 29, 30],
        # 7th batch also includes 1..5 (already evicted earlier) to verify evicted memory holds
        107: [1, 2, 6, 7, 31, 32, 33, 34, 35],
    }

    def fake_find(artist, title, conn=None):
        return seed_map.get((artist, title))

    def fake_suggest(seed_id, limit=10, **kw):
        return [_FakeSuggestion(tid) for tid in pool_by_seed.get(seed_id, [])]

    with patch.object(localcache, "find_track_id", side_effect=fake_find), \
         patch.object(localcache, "find_track_id_relaxed", side_effect=fake_find), \
         patch.object(queue_suggest, "suggest_for_track", side_effect=fake_suggest):
        for idx in range(1, 6):
            res = collection.radio_keep_going_refill(f"RadioArtist{idx}", f"RadioTitle{idx}")
            assert res["ok"] is True
            assert res["added"] == 5
            assert res["rotated"] == 0
            assert res["total"] == idx * 5
            # Repeating the same detected song is a no-op
            dup = collection.radio_keep_going_refill(f"RadioArtist{idx}", f"RadioTitle{idx}")
            assert dup["added"] == 0 and dup["reason"] == "unchanged_song"

        ids_at_25, _ = _ids()
        assert ids_at_25 == list(range(1, 26))
        assert len(set(ids_at_25)) == 25

        # 6th detection rotates out 1..5 and appends 26..30
        res6 = collection.radio_keep_going_refill("RadioArtist6", "RadioTitle6")
        assert res6["ok"] is True and res6["added"] == 5 and res6["rotated"] == 5 and res6["total"] == 25
        ids_after_6, _ = _ids()
        assert ids_after_6 == list(range(6, 31))

        # 7th detection rotates out 6..10 and appends 31..35 (still never re-adding 1..5)
        res7 = collection.radio_keep_going_refill("RadioArtist7", "RadioTitle7")
        assert res7["ok"] is True and res7["added"] == 5 and res7["rotated"] == 5 and res7["total"] == 25
        ids_after_7, _ = _ids()
        assert ids_after_7 == list(range(11, 36))


def test_radio_keep_going_unknown_song_retries_on_next_detection(fake_library):
    """If the detected song isn't in the library or yields 0 matches, leave queue alone and succeed on next song."""
    from karaoke import queue_suggest

    localcache.set_queue_autopilot(True)
    with patch.object(localcache, "find_track_id", return_value=None), \
         patch.object(localcache, "find_track_id_relaxed", return_value=None):
        miss = collection.radio_keep_going_refill("Unknown Band", "Unknown Track")
    assert miss["added"] == 0 and miss["reason"] == "seed_not_found"
    assert localcache.load_active_queue()["rows"] == []

    # Next detection changes to a known track -> seeds 5 songs
    with patch.object(localcache, "find_track_id", return_value=200), \
         patch.object(queue_suggest, "suggest_for_track",
                      return_value=[_FakeSuggestion(i) for i in range(41, 48)]):
        hit = collection.radio_keep_going_refill("Known Band", "Known Track")
    assert hit["ok"] is True and hit["added"] == 5 and hit["total"] == 5
    assert _ids() == ([41, 42, 43, 44, 45], 0)


def test_radio_keep_going_preserves_user_playlist_and_honors_autopilot_flag(fake_library):
    """Never clobber a non-empty user playlist, and do nothing when Keep Going is off."""
    _seed([1, 2, 3], 0)  # playlist_id == 'p1' (user queue)
    localcache.set_queue_autopilot(True)
    res = collection.radio_keep_going_refill("Any", "Song")
    assert res["added"] == 0 and res["reason"] == "queue_not_empty"
    assert _ids() == ([1, 2, 3], 0)

    # Empty queue but Keep Going is off
    localcache.save_active_queue("", "", [], current_index=0)
    localcache.set_queue_autopilot(False)
    res_off = collection.radio_keep_going_refill("Any", "Song")
    assert res_off["added"] == 0 and res_off["reason"] == "disabled"


def test_radio_keep_going_dedups_by_artist_title_across_duplicate_track_ids(fake_library):
    """Two library rows with different track_ids but the same (artist, title) count as a double."""
    from karaoke import queue_suggest

    localcache.set_queue_autopilot(True)
    dup_rows = [
        {"track_id": 10, "artist": "Bowie", "title": "Heroes", "url": "u10", "kind": "youtube"},
        {"track_id": 11, "artist": "bowie", "title": "HEROES", "url": "u11", "kind": "youtube"},
        {"track_id": 12, "artist": "Bowie", "title": "Starman", "url": "u12", "kind": "youtube"},
    ]
    with patch.object(localcache, "find_track_id", return_value=99), \
         patch.object(queue_suggest, "suggest_for_track",
                      return_value=[_FakeSuggestion(r["track_id"], r["artist"], r["title"]) for r in dup_rows]), \
         patch.object(collection, "rows_for", return_value=dup_rows):
        res = collection.radio_keep_going_refill("SeedArtist", "SeedTitle")
    assert res["ok"] is True and res["added"] == 2
    assert _ids() == ([10, 12], 0)


def test_radio_keep_going_waits_for_new_song_vectors_while_still_heard(fake_library):
    """A just-discovered song has no vectors yet: retry until suggestions appear."""
    from karaoke import queue_suggest

    localcache.set_queue_autopilot(True)
    live_queue._RADIO_LAST_SEED_KEY = None
    calls = {"n": 0}

    def slow_suggest(seed_id, limit=10, **kw):
        calls["n"] += 1
        return [] if calls["n"] < 3 else [_FakeSuggestion(i) for i in range(61, 68)]

    with patch.object(localcache, "find_track_id", return_value=300), \
         patch.object(queue_suggest, "suggest_for_track", side_effect=slow_suggest):
        res = collection.radio_keep_going_refill(
            "New", "Song", wait_for_ready_s=5, poll_interval_s=0.01, still_current=lambda: True)
    assert res["added"] == 5 and calls["n"] == 3


def test_radio_keep_going_stops_waiting_when_song_changes(fake_library):
    from karaoke import queue_suggest

    localcache.set_queue_autopilot(True)
    with patch.object(localcache, "find_track_id", return_value=300), \
         patch.object(queue_suggest, "suggest_for_track", return_value=[]):
        res = collection.radio_keep_going_refill(
            "New", "Song", wait_for_ready_s=60, poll_interval_s=0.01, still_current=lambda: False)
    assert res["reason"] == "song_changed"
    assert localcache.load_active_queue()["rows"] == []
