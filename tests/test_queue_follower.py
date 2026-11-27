"""Unit tests for the local queue follower helpers."""
from unittest.mock import patch

from karaoke import queue_follower as qf


def test_queue_signature_ignores_length_but_not_identity():
    # Queueing/removing songs (length change) must NOT read as a new queue --
    # that used to restart the song already playing.
    a = {"playlist_id": "p1", "name": "Hip Hop", "rows": [{}, {}]}
    b = {"playlist_id": "p1", "name": "Hip Hop", "rows": [{}, {}, {}]}
    c = {"playlist_id": "p2", "name": "Hip Hop", "rows": [{}, {}]}
    assert qf._queue_signature(None) is None
    assert qf._queue_signature(a) == qf._queue_signature(b)
    assert qf._queue_signature(a) != qf._queue_signature(c)   # playlist differs


def test_row_key_identifies_the_song_not_the_index():
    assert qf._row_key({"track_id": 5, "url": "u"}) == qf._row_key({"track_id": 5, "url": "u", "bpm": 1})
    assert qf._row_key({"track_id": 5, "url": "u"}) != qf._row_key({"track_id": 6, "url": "u"})


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


def test_save_index_only_moves_the_index():
    q = {"playlist_id": "p1", "name": "Hip Hop", "rows": [{"a": 1}], "current_index": 0}
    with patch.object(qf.localcache, "set_active_queue_index") as set_idx, \
         patch.object(qf.localcache, "save_active_queue") as save:
        qf._save_index(q, 3)
    set_idx.assert_called_once_with(3)
    save.assert_not_called()   # never re-save a possibly stale row list


def test_autopilot_tick_forces_ytmusic_autoplay_off_when_enabled():
    q = {"playlist_id": "p1", "name": "Hip Hop", "autopilot": True, "rows": [{"track_id": 1}]}
    with patch.object(qf.localcache, "connect") as connect, \
         patch.object(qf.localcache, "load_active_queue", return_value=q), \
         patch.object(qf, "cdp_set_ytmusic_autoplay") as set_autoplay, \
         patch("karaoke.collection.autopilot_refill"):
        connect.return_value.__enter__.return_value = object()
        qf._maybe_autopilot_refill()
    set_autoplay.assert_called_once_with(False)


def test_autopilot_tick_leaves_ytmusic_alone_when_disabled():
    q = {"playlist_id": "p1", "name": "Hip Hop", "autopilot": False, "rows": [{"track_id": 1}]}
    with patch.object(qf.localcache, "connect") as connect, \
         patch.object(qf.localcache, "load_active_queue", return_value=q), \
         patch.object(qf, "cdp_set_ytmusic_autoplay") as set_autoplay, \
         patch("karaoke.collection.autopilot_refill"):
        connect.return_value.__enter__.return_value = object()
        qf._maybe_autopilot_refill()
    set_autoplay.assert_not_called()
