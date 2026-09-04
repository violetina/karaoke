"""Decompiling a recording into the database.

The subtle part is that segment boundaries live on the *track's* timeline, not
the recording's: a marker saying "290s into this song" dates its start to before
the recording may have begun. Slices must be clamped to the audio that exists.
"""
from pathlib import Path

import pytest

from karaoke import recording_worker as rw
from karaoke.recording_slice import Segment


def _seg(start, end, artist="A", title="B", marks=2, spread=0.0):
    return Segment(artist=artist, title=title, start_wall=start, end_wall=end,
                   marks=marks, spread=spread)


def _file(start, duration, name="seg-20260904-210303.flac"):
    return rw.SegmentFile(path=Path(f"/tmp/{name}"), start_wall=start,
                          duration=duration)


# -- clamping to the captured audio -------------------------------------

def test_a_track_starting_before_the_recording_is_clamped():
    """The real case: recording joined a song already 290s in."""
    segment = _seg(1000.0, 1600.0)          # track ran 1000..1600
    span = (1290.0, 1400.0)                 # only this was captured
    assert rw.clamp(segment, span) == (1290.0, 1400.0)


def test_a_track_ending_after_the_recording_is_clamped():
    assert rw.clamp(_seg(1000.0, 1600.0), (900.0, 1200.0)) == (1000.0, 1200.0)


def test_a_fully_captured_track_is_untouched():
    assert rw.clamp(_seg(1000.0, 1200.0), (900.0, 1300.0)) == (1000.0, 1200.0)


def test_too_little_captured_audio_is_refused():
    """Analysing a 5s fragment would produce a confident-looking wrong key."""
    assert rw.clamp(_seg(1000.0, 1600.0), (1595.0, 1600.0)) is None


def test_no_overlap_at_all_is_refused():
    assert rw.clamp(_seg(1000.0, 1100.0), (2000.0, 2500.0)) is None


def test_the_clamp_floor_matches_the_sampler():
    from karaoke import sample_audio
    assert rw.MIN_AUDIO_S == sample_audio.MIN_SECONDS


# -- placing files on the wall clock ------------------------------------

def test_recording_span_covers_every_file():
    files = [_file(1000.0, 600.0), _file(1600.0, 300.0)]
    assert rw.recording_span(files) == (1000.0, 1900.0)


def test_recording_span_of_nothing_is_none():
    assert rw.recording_span([]) is None


def test_segment_files_reads_the_wall_clock_from_the_name(tmp_path, monkeypatch):
    """Capture names each file for when it was opened, which is the timeline."""
    (tmp_path / "seg-20260904-210303.flac").write_bytes(b"x")
    monkeypatch.setattr(rw, "probe_duration", lambda p: 600.0)
    files = rw.segment_files(tmp_path)
    assert len(files) == 1
    import time
    expected = time.mktime(time.strptime("20260904210303", "%Y%m%d%H%M%S"))
    assert files[0].start_wall == expected
    assert files[0].end_wall == expected + 600.0


def test_unreadable_segments_are_skipped(tmp_path, monkeypatch):
    """Usually the file being written when the process died."""
    (tmp_path / "seg-20260904-210303.flac").write_bytes(b"x")
    monkeypatch.setattr(rw, "probe_duration", lambda p: None)
    assert rw.segment_files(tmp_path) == []


def test_unrelated_files_are_ignored(tmp_path, monkeypatch):
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "seg-bad-name.flac").write_bytes(b"x")
    monkeypatch.setattr(rw, "probe_duration", lambda p: 60.0)
    assert rw.segment_files(tmp_path) == []


# -- cutting ------------------------------------------------------------

def test_cut_selects_only_overlapping_files(tmp_path, monkeypatch):
    """A track straddling two segments must concatenate exactly those two."""
    files = [_file(1000.0, 600.0, "seg-a.flac"),
             _file(1600.0, 600.0, "seg-b.flac"),
             _file(2200.0, 600.0, "seg-c.flac")]
    seen = {}
    dest = tmp_path / "out.wav"

    def fake_run(cmd, **kw):
        listing = Path(cmd[cmd.index("-i") + 1])
        seen["files"] = listing.read_text()
        seen["cmd"] = cmd
        dest.write_bytes(b"RIFF")
        import subprocess
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(rw.subprocess, "run", fake_run)
    assert rw.cut(files, 1500.0, 1700.0, dest) is True
    assert "seg-a.flac" in seen["files"] and "seg-b.flac" in seen["files"]
    assert "seg-c.flac" not in seen["files"]
    # Seek is relative to the first overlapping file, not the wall clock.
    assert f"{500.0:.3f}" in seen["cmd"]


def test_cut_with_no_overlapping_audio_fails(tmp_path):
    files = [_file(1000.0, 60.0, "seg-a.flac")]
    assert rw.cut(files, 5000.0, 5100.0, tmp_path / "o.wav") is False


def test_cut_of_zero_length_fails(tmp_path):
    files = [_file(1000.0, 600.0, "seg-a.flac")]
    assert rw.cut(files, 1200.0, 1200.0, tmp_path / "o.wav") is False


# -- the method marker --------------------------------------------------

def test_recording_analyses_are_marked_as_such():
    """A recording-derived key must never read as one from a real master."""
    assert rw.METHOD_SUFFIX == "+recording"
