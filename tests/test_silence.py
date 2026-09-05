"""Finding the silent stretches in a recording.

A capture is not all music. YouTube Music pauses a device when the account
starts playing elsewhere, and that happened mid-album: identification succeeded
until 15:21 and failed on every attempt after, because there was nothing to
hear. 14.5 of 36 minutes were silence.
"""
import subprocess

import pytest

from karaoke import silence
from karaoke.silence import Silence


def _stderr(text):
    return subprocess.CompletedProcess([], 0, "", text)


# -- parsing silencedetect ----------------------------------------------

def test_a_pair_of_markers_is_one_stretch(monkeypatch, tmp_path):
    out = ("[silencedetect] silence_start: 98.5\n"
           "[silencedetect] silence_end: 599.6 | silence_duration: 501.1\n")
    monkeypatch.setattr(silence.subprocess, "run", lambda *a, **k: _stderr(out))
    found = silence.detect(tmp_path / "x.flac")
    assert len(found) == 1
    assert found[0].start == pytest.approx(98.5)
    assert found[0].duration == pytest.approx(501.1)


def test_several_stretches_are_paired_in_order(monkeypatch, tmp_path):
    out = ("silence_start: 10.0\nsilence_end: 13.0\n"
           "silence_start: 35.0\nsilence_end: 40.0\n")
    monkeypatch.setattr(silence.subprocess, "run", lambda *a, **k: _stderr(out))
    found = silence.detect(tmp_path / "x.flac")
    assert [(s.start, s.end) for s in found] == [(10.0, 13.0), (35.0, 40.0)]


def test_a_stretch_still_open_at_the_end_is_closed(monkeypatch, tmp_path):
    """The most interesting case: a session left running after playback
    stopped has no silence_end at all."""
    monkeypatch.setattr(silence.subprocess, "run",
                        lambda *a, **k: _stderr("silence_start: 120.0\n"))
    monkeypatch.setattr(silence, "measured_duration", lambda p: 600.0)
    found = silence.detect(tmp_path / "x.flac")
    assert len(found) == 1
    assert found[0].end == pytest.approx(600.0)


def test_an_open_stretch_with_no_known_duration_is_dropped(monkeypatch, tmp_path):
    """Better to report no silence than to invent its length."""
    monkeypatch.setattr(silence.subprocess, "run",
                        lambda *a, **k: _stderr("silence_start: 120.0\n"))
    monkeypatch.setattr(silence, "measured_duration", lambda p: None)
    assert silence.detect(tmp_path / "x.flac") == []


def test_an_unreadable_file_reports_no_silence(monkeypatch, tmp_path):
    """Absence of evidence must never be reported as silence -- the caller may
    be about to skip or delete whatever it covers."""
    def boom(*a, **k):
        raise OSError("no ffmpeg")

    monkeypatch.setattr(silence.subprocess, "run", boom)
    assert silence.detect(tmp_path / "x.flac") == []


def test_a_silent_file_yields_nothing_when_quiet_is_not_detected(monkeypatch,
                                                                 tmp_path):
    monkeypatch.setattr(silence.subprocess, "run", lambda *a, **k: _stderr(""))
    assert silence.detect(tmp_path / "x.flac") == []


# -- totals and the audible span ----------------------------------------

def test_total_silence_sums_the_stretches():
    assert silence.total_silence([Silence(0.0, 3.0), Silence(10.0, 15.0)]) \
        == pytest.approx(8.0)


def test_the_longest_audible_run_is_found():
    """What to transcribe when a session is part music and part nothing."""
    gaps = [Silence(0.0, 60.0), Silence(300.0, 360.0)]
    assert silence.loudest_span(gaps, 900.0) == (360.0, 900.0)


def test_a_gap_at_the_start_does_not_hide_the_music():
    gaps = [Silence(0.0, 360.0)]
    assert silence.loudest_span(gaps, 2160.0) == (360.0, 2160.0)


def test_no_silence_means_the_whole_file():
    assert silence.loudest_span([], 120.0) == (0.0, 120.0)


def test_an_unknown_duration_has_no_span():
    assert silence.loudest_span([], 0.0) is None


def test_overlapping_gaps_do_not_confuse_the_span():
    gaps = [Silence(10.0, 50.0), Silence(30.0, 70.0)]
    assert silence.loudest_span(gaps, 100.0) == (70.0, 100.0)


def test_describe_reports_position_and_length():
    rows = silence.describe([Silence(98.0, 599.0)])
    assert "1:38" in rows[0] and "501" in rows[0]
