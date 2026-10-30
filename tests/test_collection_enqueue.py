"""Unit tests for collection.enqueue_next (local 'Queue next')."""
from unittest.mock import patch

from karaoke import collection


_ROW = {"track_id": 99, "artist": "Queen", "title": "Flash",
        "url": "https://music.youtube.com/watch?v=z", "kind": "youtube_music",
        "bpm": None, "key": None, "energy": None}


def test_enqueue_next_inserts_right_after_current_index():
    existing = {
        "playlist_id": "p1", "name": "Hip Hop", "current_index": 1,
        "rows": [
            {"track_id": 1, "artist": "A", "title": "One"},
            {"track_id": 2, "artist": "B", "title": "Two"},   # <- current
            {"track_id": 3, "artist": "C", "title": "Three"},
        ],
    }
    saved = {}

    def fake_save(pid, name, rows, current_index=0, conn=None):
        saved["rows"] = rows
        saved["current_index"] = current_index

    with patch.object(collection, "rows_for", return_value=[_ROW]), \
         patch.object(collection.localcache, "load_active_queue", return_value=existing), \
         patch.object(collection.localcache, "save_active_queue", side_effect=fake_save), \
         patch.object(collection.localcache, "record_queue_event"):
        out = collection.enqueue_next(99, conn=object())

    assert out["status"] == "queued"
    # Inserted at index 2 (right after current index 1).
    assert [r["track_id"] for r in saved["rows"]] == [1, 2, 99, 3]
    # The playing position is preserved.
    assert saved["current_index"] == 1


def test_enqueue_next_creates_queue_when_none_loaded():
    saved = {}

    def fake_save(pid, name, rows, current_index=0, conn=None):
        saved["name"] = name
        saved["rows"] = rows

    with patch.object(collection, "rows_for", return_value=[_ROW]), \
         patch.object(collection.localcache, "load_active_queue", return_value=None), \
         patch.object(collection.localcache, "save_active_queue", side_effect=fake_save), \
         patch.object(collection.localcache, "record_queue_event"):
        out = collection.enqueue_next(99, conn=object())

    assert out["status"] == "queued"
    assert out.get("created") is True
    assert [r["track_id"] for r in saved["rows"]] == [99]


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
    saved = {}

    def fake_save(pid, name, merged, current_index=0, conn=None):
        saved["rows"] = merged
        saved["current_index"] = current_index

    new_rows = [
        {"track_id": 101, "artist": "X", "title": "New1", "url": "u1", "kind": "youtube"},
        {"track_id": 102, "artist": "Y", "title": "New2", "url": "u2", "kind": "youtube"},
    ]
    with patch.object(collection.localcache, "load_active_queue",
                      return_value=_queue(rows, 4, True)), \
         patch("karaoke.queue_suggest.suggest_for_queue",
               return_value=[_Pick(101), _Pick(102)]), \
         patch.object(collection, "rows_for", return_value=new_rows), \
         patch.object(collection.localcache, "save_active_queue", side_effect=fake_save), \
         patch.object(collection.localcache, "record_queue_event"):
        out = collection.autopilot_refill(object(), threshold=5, add=5)

    assert out["refilled"] == 2
    assert [r["track_id"] for r in saved["rows"]] == [0, 1, 2, 3, 4, 5, 101, 102]
    assert saved["current_index"] == 4   # playing position preserved

