"""Unit tests for collection.enqueue_next (local 'Queue next')."""
from unittest.mock import patch

from karaoke import collection


_ROW = {"track_id": 99, "artist": "Queen", "title": "Flash",
        "url": "https://music.youtube.com/watch?v=z", "kind": "youtube_music",
        "bpm": None, "key": None, "energy": None}


def _mem_queue(state):
    """Run live-queue mutations against an in-memory state dict."""
    return patch.object(collection.localcache, "mutate_active_queue",
                        side_effect=lambda fn, conn=None: fn(state))


def test_enqueue_next_inserts_right_after_current_index():
    state = {
        "playlist_id": "p1", "name": "Hip Hop", "current_index": 1,
        "rows": [
            {"track_id": 1, "artist": "A", "title": "One"},
            {"track_id": 2, "artist": "B", "title": "Two"},   # <- current
            {"track_id": 3, "artist": "C", "title": "Three"},
        ],
    }
    with patch.object(collection, "rows_for", return_value=[_ROW]), _mem_queue(state), \
         patch.object(collection.localcache, "record_queue_event"):
        out = collection.enqueue_next(99, conn=object())

    assert out["status"] == "queued" and out["position"] == 3
    # Inserted at index 2 (right after current index 1).
    assert [r["track_id"] for r in state["rows"]] == [1, 2, 99, 3]
    # The playing position is preserved.
    assert state["current_index"] == 1


def test_enqueue_next_creates_queue_when_none_loaded():
    state = {"playlist_id": "", "name": "", "current_index": 0, "rows": []}
    with patch.object(collection, "rows_for", return_value=[_ROW]), _mem_queue(state), \
         patch.object(collection.localcache, "record_queue_event"):
        out = collection.enqueue_next(99, conn=object())

    assert out["status"] == "queued"
    assert out.get("created") is True
    assert state["name"] == "Karaoke: Dashboard Queue"
    assert [r["track_id"] for r in state["rows"]] == [99]


def test_enqueue_next_reports_unknown_track():
    with patch.object(collection, "rows_for", return_value=[]):
        out = collection.enqueue_next(12345, conn=object())
    assert out["status"] == "error"


# --- autopilot_refill ("keep going") -------------------------------------

class _Pick:
    def __init__(self, track_id):
        self.track_id = track_id


def _queue(rows, current_index, autopilot):
    return {"playlist_id": "p1", "name": "Hip Hop", "current_index": current_index,
            "autopilot": autopilot, "rows": rows}


def test_autopilot_refill_noop_when_disabled():
    rows = [{"track_id": i} for i in range(3)]
    with patch.object(collection.localcache, "load_active_queue",
                      return_value=_queue(rows, 2, False)):
        out = collection.autopilot_refill(object())
    assert out["refilled"] == 0 and out["reason"] == "disabled"


def test_autopilot_refill_noop_when_runway_remains():
    rows = [{"track_id": i} for i in range(20)]
    with patch.object(collection.localcache, "load_active_queue",
                      return_value=_queue(rows, 2, True)):
        out = collection.autopilot_refill(object(), threshold=5)
    assert out["refilled"] == 0 and out["reason"] == "has_runway"


def test_autopilot_refill_appends_fitting_tracks_near_end():
    rows = [{"track_id": i} for i in range(6)]  # remaining after idx 4 == 1
    state = _queue([dict(r) for r in rows], 4, True)

    new_rows = [
        {"track_id": 101, "artist": "X", "title": "New1", "url": "u1", "kind": "youtube"},
        {"track_id": 102, "artist": "Y", "title": "New2", "url": "u2", "kind": "youtube"},
    ]
    with patch.object(collection.localcache, "load_active_queue",
                      return_value=_queue(rows, 4, True)), \
         patch("karaoke.queue_suggest.suggest_for_queue",
               return_value=[_Pick(101), _Pick(102)]), \
         patch.object(collection, "rows_for", return_value=new_rows), \
         _mem_queue(state), \
         patch.object(collection.localcache, "record_queue_event"):
        out = collection.autopilot_refill(object(), threshold=5, add=5)

    assert out["refilled"] == 2
    assert [r["track_id"] for r in state["rows"]] == [0, 1, 2, 3, 4, 5, 101, 102]
    assert state["current_index"] == 4   # playing position preserved


# --- advance_active_queue (queue-aware Next/Previous) ---------------------

def test_advance_active_queue_moves_index_forward():
    rows = [{"track_id": i, "artist": "A", "title": f"T{i}"} for i in range(5)]
    state = _queue(rows, 2, False)
    with _mem_queue(state):
        res = collection.advance_active_queue(object(), 1)
    assert res["index"] == 3 and res["moved"] is True
    assert state["current_index"] == 3


def test_advance_active_queue_clamps_at_end():
    rows = [{"track_id": i, "artist": "A", "title": f"T{i}"} for i in range(3)]
    state = _queue(rows, 2, False)
    with _mem_queue(state):
        res = collection.advance_active_queue(object(), 1)
    assert res["index"] == 2 and res["moved"] is False


def test_advance_active_queue_none_without_queue():
    state = {"playlist_id": "", "name": "", "current_index": 0, "rows": []}
    with _mem_queue(state):
        assert collection.advance_active_queue(object(), 1) is None
