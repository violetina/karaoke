"""Which file on disk counts as a track's audio (chords, mood timelines)."""
from unittest.mock import MagicMock

from karaoke import chord_timeline


def _conn(duration, sources):
    conn = MagicMock()
    track = MagicMock()
    track.fetchone.return_value = {"duration": duration}
    srcs = MagicMock()
    srcs.fetchall.return_value = [{"kind": k, "url": u} for k, u in sources]
    conn.execute.side_effect = [track, srcs]
    return conn


def _files(tmp_path, monkeypatch, lengths):
    paths = {}
    for name, seconds in lengths.items():
        p = tmp_path / name
        p.write_bytes(b"x")
        paths[name] = p
    monkeypatch.setattr(chord_timeline, "_audio_seconds",
                        lambda path: lengths.get(path.name))
    return paths


def test_a_one_second_sample_clip_is_skipped_for_the_whole_song(tmp_path, monkeypatch):
    f = _files(tmp_path, monkeypatch, {"04.mp3": 1.0, "Bohemian Rhapsody.mp3": 355.0})
    conn = _conn(355.0, [("local", str(f["04.mp3"])), ("local", str(f["Bohemian Rhapsody.mp3"]))])
    assert chord_timeline.local_audio(146, conn) == f["Bohemian Rhapsody.mp3"]


def test_a_file_far_shorter_than_the_track_is_not_the_track(tmp_path, monkeypatch):
    f = _files(tmp_path, monkeypatch, {"cut.mp3": 120.0})
    conn = _conn(355.0, [("local", str(f["cut.mp3"]))])
    assert chord_timeline.local_audio(146, conn) is None


def test_radio_captures_are_not_used_even_when_they_are_all_there_is(tmp_path, monkeypatch):
    f = _files(tmp_path, monkeypatch, {"Queen - Bohemian Rhapsody.flac": 355.0})
    conn = _conn(355.0, [("radio_capture", str(f["Queen - Bohemian Rhapsody.flac"]))])
    assert chord_timeline.local_audio(146, conn) is None


def test_an_unreadable_header_is_left_to_the_decoder(tmp_path, monkeypatch):
    f = _files(tmp_path, monkeypatch, {"odd.mp3": None})
    conn = _conn(200.0, [("local", str(f["odd.mp3"]))])
    assert chord_timeline.local_audio(1, conn) == f["odd.mp3"]
