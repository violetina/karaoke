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
