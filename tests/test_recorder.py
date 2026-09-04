"""Record mode capture: the session lifecycle and marker storage.

ffmpeg and songrec are faked throughout -- this covers the bookkeeping, not the
audio, which was verified separately against live playback.
"""
import subprocess
import time
from types import SimpleNamespace

import pytest

from karaoke import localcache, recorder


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    real = localcache.connect
    monkeypatch.setattr(localcache, "connect", lambda *a, **k: real(path))
    monkeypatch.setattr(recorder, "recordings_dir", lambda: tmp_path / "rec")
    return path


class _FakeProc:
    def __init__(self, alive=True):
        self._alive = alive
        self.terminated = False

    def poll(self):
        return None if self._alive else 1

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._alive = False


def _no_audio_loop(monkeypatch):
    """Stop the identification thread from doing anything real."""
    monkeypatch.setattr(recorder, "_identify_loop", lambda session: None)


def test_start_records_the_playing_monitor(db, monkeypatch):
    """Never a microphone: record mode captures the machine's own output."""
    _no_audio_loop(monkeypatch)
    monkeypatch.setattr(recorder.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    monkeypatch.setattr("karaoke.sample_audio.monitor_source",
                        lambda sink="": "bluez.monitor")
    seen = {}

    def fake_popen(cmd, **kw):
        seen["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(recorder.subprocess, "Popen", fake_popen)
    session = recorder.start()
    try:
        assert session.source == "bluez.monitor"
        assert "bluez.monitor" in seen["cmd"]
        assert "flac" in " ".join(seen["cmd"])
        assert session.directory.is_dir()
    finally:
        recorder.stop(session.recording_id)


def test_start_refuses_when_nothing_is_playing(db, monkeypatch):
    monkeypatch.setattr(recorder.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    monkeypatch.setattr("karaoke.sample_audio.monitor_source", lambda sink="": "")
    with pytest.raises(recorder.RecorderError, match="nothing is playing"):
        recorder.start()


def test_start_reports_a_missing_ffmpeg(db, monkeypatch):
    monkeypatch.setattr(recorder.shutil, "which", lambda n: None)
    with pytest.raises(recorder.RecorderError, match="ffmpeg"):
        recorder.start()


def test_stop_closes_the_row_and_finalises_the_segment(db, monkeypatch):
    """SIGTERM, not kill: ffmpeg must finalise the FLAC it is writing."""
    _no_audio_loop(monkeypatch)
    monkeypatch.setattr(recorder.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    monkeypatch.setattr("karaoke.sample_audio.monitor_source",
                        lambda sink="": "x.monitor")
    proc = _FakeProc()
    monkeypatch.setattr(recorder.subprocess, "Popen", lambda cmd, **kw: proc)

    session = recorder.start()
    recorder.stop(session.recording_id)

    assert proc.terminated is True
    assert not recorder.is_running(session.recording_id)
    with localcache.connect() as c:
        row = c.execute("SELECT status, ended_at FROM recordings WHERE recording_id=?",
                        (session.recording_id,)).fetchone()
    assert row["status"] == "complete" and row["ended_at"] is not None


# -- markers ------------------------------------------------------------

def test_a_successful_identification_is_stored(db):
    ref = SimpleNamespace(artist="Portishead", title="Glory Box", offset=30.0)
    recorder.add_mark(1, ref)
    marks = recorder.load_marks(1)
    assert len(marks) == 1
    assert marks[0].ok and marks[0].title == "Glory Box"
    assert marks[0].at_offset == 30.0
    assert marks[0].start_estimate == pytest.approx(marks[0].at_wall - 30.0)


def test_a_failed_identification_is_stored_too(db):
    """A gap is evidence about the recording, not something to discard."""
    recorder.add_mark(1, None)
    marks = recorder.load_marks(1)
    assert len(marks) == 1 and marks[0].ok is False


def test_an_empty_title_counts_as_a_failure(db):
    recorder.add_mark(1, SimpleNamespace(artist="", title="", offset=None))
    assert recorder.load_marks(1)[0].ok is False


def test_mark_count_separates_hits_from_attempts(db):
    ref = SimpleNamespace(artist="A", title="B", offset=1.0)
    recorder.add_mark(1, ref)
    recorder.add_mark(1, None)
    recorder.add_mark(1, ref)
    assert recorder.mark_count(1) == (2, 3)


def test_marks_come_back_in_time_order(db):
    ref = SimpleNamespace(artist="A", title="B", offset=1.0)
    for _ in range(3):
        recorder.add_mark(1, ref)
        time.sleep(0.01)
    marks = recorder.load_marks(1)
    assert [m.at_wall for m in marks] == sorted(m.at_wall for m in marks)


def test_marks_of_other_recordings_are_not_returned(db):
    ref = SimpleNamespace(artist="A", title="B", offset=1.0)
    recorder.add_mark(1, ref)
    recorder.add_mark(2, ref)
    assert len(recorder.load_marks(1)) == 1


# -- limits -------------------------------------------------------------

def test_a_session_over_its_time_cap_is_stopped(db, monkeypatch):
    session = recorder.Session(
        recording_id=1, directory=db.parent, source="x",
        process=_FakeProc(), stop=__import__("threading").Event(),
        started_mono=time.monotonic() - (recorder.MAX_HOURS * 3600.0 + 10))
    assert recorder._over_limit(session) is True


def test_a_session_over_its_size_cap_is_stopped(db, monkeypatch):
    monkeypatch.setattr(recorder, "directory_size",
                        lambda d: recorder.MAX_BYTES + 1)
    session = recorder.Session(
        recording_id=1, directory=db.parent, source="x",
        process=_FakeProc(), stop=__import__("threading").Event(),
        started_mono=time.monotonic())
    assert recorder._over_limit(session) is True


def test_a_fresh_session_is_within_limits(db, monkeypatch):
    monkeypatch.setattr(recorder, "directory_size", lambda d: 1024)
    session = recorder.Session(
        recording_id=1, directory=db.parent, source="x",
        process=_FakeProc(), stop=__import__("threading").Event(),
        started_mono=time.monotonic())
    assert recorder._over_limit(session) is False


# -- the TUI key --------------------------------------------------------

def test_record_is_bound_to_O():
    from karaoke.tui import KaraokeTui, binding_rows
    assert dict(binding_rows(KaraokeTui.BINDINGS))["O"] == "Record"
