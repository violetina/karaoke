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
