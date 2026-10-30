"""Unit tests for the local queue follower helpers."""
from unittest.mock import patch

from karaoke import queue_follower as qf


def test_queue_signature_changes_with_identity_and_length():
    a = {"playlist_id": "p1", "name": "Hip Hop", "rows": [{}, {}]}
    b = {"playlist_id": "p1", "name": "Hip Hop", "rows": [{}, {}, {}]}
    c = {"playlist_id": "p2", "name": "Hip Hop", "rows": [{}, {}]}
    assert qf._queue_signature(None) is None
    assert qf._queue_signature(a) == qf._queue_signature(dict(a))
    assert qf._queue_signature(a) != qf._queue_signature(b)   # length differs
    assert qf._queue_signature(a) != qf._queue_signature(c)   # playlist differs


def test_play_row_skips_when_no_source_url():
    with patch.object(qf, "open_song_url") as open_url:
        assert qf._play_row({"artist": "A", "title": "T", "url": ""}) is False
        open_url.assert_not_called()


def test_play_row_opens_known_source():
    row = {"artist": "Queen", "title": "Flash", "url": "https://music.youtube.com/watch?v=x", "kind": "youtube_music"}
    with patch.object(qf, "open_song_url", return_value=1234) as open_url:
        assert qf._play_row(row) is True
        open_url.assert_called_once()
        args, kwargs = open_url.call_args
        assert args[0] == row["url"]
        assert kwargs["artist"] == "Queen"
        assert kwargs["title"] == "Flash"


def test_save_index_persists_current_index():
    q = {"playlist_id": "p1", "name": "Hip Hop", "rows": [{"a": 1}], "current_index": 0}
    with patch.object(qf.localcache, "save_active_queue") as save:
        qf._save_index(q, 3)
        save.assert_called_once()
        _, kwargs = save.call_args
        assert kwargs["current_index"] == 3
