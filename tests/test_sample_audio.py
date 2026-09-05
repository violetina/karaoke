"""Recording the playing output to analyse tracks that have no file.

Spotify playback has no downloadable audio, so key/BPM analysis has nothing to
work on and those tracks sit in the backlog forever. Capturing the sink monitor
closes that gap; these tests cover the parts that do not need real audio.
"""
import subprocess

import pytest

from karaoke import sample_audio


class _FakePactl:
    def __init__(self, sink_inputs="", sinks="", default=""):
        self.sink_inputs, self.sinks, self.default = sink_inputs, sinks, default

    def __call__(self, *args):
        if args[:3] == ("list", "short", "sink-inputs"):
            return self.sink_inputs
        if args[:3] == ("list", "short", "sinks"):
            return self.sinks
        if args[:1] == ("get-default-sink",):
            return self.default
        return ""


def test_playing_sink_prefers_the_one_carrying_audio(monkeypatch):
    """With a Bluetooth speaker paired alongside built-in output, the default
    sink is regularly not where the music is routed."""
    monkeypatch.setattr(sample_audio, "_pactl", _FakePactl(
        sink_inputs="5517\t1683\t5516\tPipeWire\tfloat32le",
        sinks="58\talsa_output.speaker\tPipeWire\n1683\tbluez_output.spk\tPipeWire",
        default="alsa_output.speaker",
    ))
    assert sample_audio.playing_sink() == "bluez_output.spk"


def test_playing_sink_falls_back_to_the_default(monkeypatch):
    monkeypatch.setattr(sample_audio, "_pactl", _FakePactl(
        sink_inputs="", sinks="58\talsa_output.speaker\tPipeWire",
        default="alsa_output.speaker"))
    assert sample_audio.playing_sink() == "alsa_output.speaker"


def test_playing_sink_is_blank_when_nothing_is_set(monkeypatch):
    monkeypatch.setattr(sample_audio, "_pactl",
                        _FakePactl(default="@DEFAULT_SINK@"))
    assert sample_audio.playing_sink() == ""


def test_monitor_source_appends_monitor(monkeypatch):
    monkeypatch.setattr(sample_audio, "playing_sink", lambda: "bluez_output.spk")
    assert sample_audio.monitor_source() == "bluez_output.spk.monitor"


def test_monitor_source_is_blank_with_no_sink(monkeypatch):
    monkeypatch.setattr(sample_audio, "playing_sink", lambda: "")
    assert sample_audio.monitor_source() == ""


def test_capture_refuses_a_too_short_excerpt():
    """Below the floor the key vote is meaningless, so do not spend the time."""
    with pytest.raises(sample_audio.CaptureError, match="at least"):
        sample_audio.capture(5.0)


def test_capture_refuses_when_nothing_is_playing(monkeypatch):
    monkeypatch.setattr(sample_audio.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(sample_audio, "monitor_source", lambda sink="": "")
    with pytest.raises(sample_audio.CaptureError, match="nothing is playing"):
        sample_audio.capture(30.0)


def test_capture_reports_a_missing_ffmpeg(monkeypatch):
    monkeypatch.setattr(sample_audio.shutil, "which", lambda n: None)
    with pytest.raises(sample_audio.CaptureError, match="ffmpeg"):
        sample_audio.capture(30.0)


class _FakePopen:
    """Stands in for the ffmpeg process.

    capture() polls rather than waits, so a track change can cut a sample
    short. That means the seam is Popen, not run -- a double built on run is
    inert and lets the real ffmpeg through.
    """

    def __init__(self, cmd, returncode=0, on_start=None, alive_polls=0, **kw):
        self.cmd = cmd
        self._returncode = returncode
        self._polls = alive_polls
        self.stdout = None
        self.stderr = _FakeStream("device busy") if returncode else _FakeStream("")
        self.terminated = False
        self.killed = False
        if on_start:
            on_start()

    def poll(self):
        if self._polls > 0:
            self._polls -= 1
            return None
        return self._returncode

    @property
    def returncode(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._polls = 0

    def kill(self):
        self.killed = True
        self._polls = 0

    def wait(self, timeout=None):
        self._polls = 0
        return self._returncode


class _FakeStream:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


def test_capture_records_from_the_monitor(monkeypatch, tmp_path):
    """The recorded source must be the monitor, never a microphone."""
    monkeypatch.setattr(sample_audio.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(sample_audio, "monitor_source",
                        lambda sink="": "bluez_output.spk.monitor")
    seen = {}
    dest = tmp_path / "s.wav"

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        return _FakePopen(cmd, on_start=lambda: dest.write_bytes(b"RIFFfake"))

    monkeypatch.setattr(sample_audio.subprocess, "Popen", fake_popen)
    sample = sample_audio.capture(30.0, dest=dest)

    assert sample.source == "bluez_output.spk.monitor"
    assert "bluez_output.spk.monitor" in seen["cmd"]
    assert "-f" in seen["cmd"] and "pulse" in seen["cmd"]
    assert sample.path == dest


def test_capture_errors_when_the_file_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(sample_audio.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(sample_audio, "monitor_source", lambda sink="": "x.monitor")
    dest = tmp_path / "s.wav"

    monkeypatch.setattr(sample_audio.subprocess, "Popen",
                        lambda cmd, **kw: _FakePopen(
                            cmd, on_start=lambda: dest.write_bytes(b"")))
    with pytest.raises(sample_audio.CaptureError, match="silent"):
        sample_audio.capture(30.0, dest=dest)


def test_capture_surfaces_an_ffmpeg_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(sample_audio.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(sample_audio, "monitor_source", lambda sink="": "x.monitor")

    monkeypatch.setattr(sample_audio.subprocess, "Popen",
                        lambda cmd, **kw: _FakePopen(cmd, returncode=1))
    with pytest.raises(sample_audio.CaptureError, match="device busy"):
        sample_audio.capture(30.0, dest=tmp_path / "s.wav")


def test_sampled_analyses_are_marked_as_such(monkeypatch, tmp_path):
    """A sampled result must never read as a full-track analysis."""
    from karaoke import localcache, track_analysis
    from karaoke.musictheory import parse_key

    db = tmp_path / "t.db"
    conn = localcache.connect(db)

    class FakeResult:
        key = parse_key("E major")
        key_confidence = 0.66
        key_agreement = "4/6"
        bpm = 123.0
        method = "essentia-edma-vote"
        energy = 0.32
        brightness = 0.224
        version = 1

    monkeypatch.setattr("karaoke.analyze.analyze_audio", lambda p: FakeResult())
    sample = sample_audio.Sample(path=tmp_path / "s.wav", seconds=45.0,
                                 source="x.monitor")
    try:
        sample_audio.analyse_sample(sample, "Ataxia", "Dust", conn=conn)
        tid = localcache.find_track_id("Ataxia", "Dust", conn)
        stored = track_analysis.get_analysis(tid, conn)
        assert stored.bpm == 123.0
        assert stored.method.endswith(sample_audio.METHOD_SUFFIX)
    finally:
        conn.close()


def test_analyse_without_a_track_does_not_store(monkeypatch, tmp_path):
    class FakeResult:
        key = None
        bpm = 120.0          # a real result, so it is not "unavailable"

    monkeypatch.setattr("karaoke.analyze.analyze_audio", lambda p: FakeResult())
    sample = sample_audio.Sample(path=tmp_path / "s.wav", seconds=45.0, source="x")
    # No artist/title: returns the analysis but touches no database.
    assert sample_audio.analyse_sample(sample) is not None


# --- the TUI key -----------------------------------------------------------

def test_sample_key_is_bound():
    from karaoke.tui import KaraokeTui, binding_rows
    assert dict(binding_rows(KaraokeTui.BINDINGS))["k"] == "Sample key/BPM"


def test_sample_key_needs_something_playing():
    from karaoke import detect
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    app._det = detect.Detection(mode="browse")
    app._sampling = False
    notes = []
    app.notify = lambda msg, **kw: notes.append(msg)
    app.action_sample_key()
    assert "Nothing playing" in notes[0]
    assert app._sampling is False


def test_sample_key_refuses_to_start_twice():
    """Capture is real time; a second run would fight the first for the source."""
    from karaoke import detect
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)
    app._det = detect.Detection(mode="spotify", artist="A", title="B")
    app._sampling = True
    notes = []
    app.notify = lambda msg, **kw: notes.append(msg)
    app.action_sample_key()
    assert "Already sampling" in notes[0]


def test_an_unavailable_analysis_is_not_stored(monkeypatch, tmp_path):
    """analyze_audio degrades to method="unavailable" rather than raising.

    Storing that writes a row with a NULL key and BPM that looks analysed and
    is then skipped by every "needs analysis" query -- worse than no row.
    """
    class Unavailable:
        key = None
        bpm = None
        method = "unavailable"

    monkeypatch.setattr("karaoke.analyze.analyze_audio", lambda p: Unavailable())
    sample = sample_audio.Sample(path=tmp_path / "s.wav", seconds=45.0, source="x")
    with pytest.raises(sample_audio.AnalysisUnavailable, match="install-audio"):
        sample_audio.analyse_sample(sample, "A", "B")


def test_a_partial_analysis_is_still_stored(monkeypatch, tmp_path):
    """librosa without essentia gives a real BPM and no key; keep the BPM."""
    from karaoke import localcache

    class PartialResult:
        key = None
        key_confidence = 0.0
        key_agreement = ""
        bpm = 129.2
        method = "unavailable"
        energy = None
        brightness = None
        version = 1

    conn = localcache.connect(tmp_path / "t.db")
    monkeypatch.setattr("karaoke.analyze.analyze_audio", lambda p: PartialResult())
    sample = sample_audio.Sample(path=tmp_path / "s.wav", seconds=45.0, source="x")
    try:
        sample_audio.analyse_sample(sample, "A", "B", conn=conn)
        tid = localcache.find_track_id("A", "B", conn)
        from karaoke import track_analysis
        assert track_analysis.get_analysis(tid, conn).bpm == 129.2
    finally:
        conn.close()


# --- stopping when the track changes --------------------------------------

def test_a_track_change_stops_the_capture(monkeypatch, tmp_path):
    """A 45-second sample easily spans a song boundary, and one that does
    would have its key, tempo and genre stored against whichever track was
    playing when it started."""
    monkeypatch.setattr(sample_audio.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(sample_audio, "monitor_source", lambda sink="": "x.monitor")
    monkeypatch.setattr(sample_audio, "POLL_SECONDS", 0.0)
    dest = tmp_path / "s.wav"
    proc = {}

    def fake_popen(cmd, **kw):
        proc["p"] = _FakePopen(cmd, alive_polls=50,
                               on_start=lambda: dest.write_bytes(b"RIFFfake"))
        return proc["p"]

    monkeypatch.setattr(sample_audio.subprocess, "Popen", fake_popen)
    with pytest.raises(sample_audio.CaptureAborted, match="changed"):
        sample_audio.capture(45.0, dest=dest, should_continue=lambda: False)

    assert proc["p"].terminated, "ffmpeg must be stopped, not left running"
    assert not dest.exists(), "a partial capture must not be left on disk"


def test_an_aborted_capture_is_not_an_error():
    """The caller asked to stop; that is not a failure to report as one."""
    assert not issubclass(sample_audio.CaptureAborted, sample_audio.CaptureError)


def test_a_capture_runs_to_completion_while_the_track_holds(monkeypatch, tmp_path):
    monkeypatch.setattr(sample_audio.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(sample_audio, "monitor_source", lambda sink="": "x.monitor")
    monkeypatch.setattr(sample_audio, "POLL_SECONDS", 0.0)
    dest = tmp_path / "s.wav"
    monkeypatch.setattr(sample_audio.subprocess, "Popen",
                        lambda cmd, **kw: _FakePopen(
                            cmd, alive_polls=3,
                            on_start=lambda: dest.write_bytes(b"RIFFfake")))

    sample = sample_audio.capture(45.0, dest=dest, should_continue=lambda: True)
    assert sample.path == dest


def test_without_a_check_the_capture_is_never_aborted(monkeypatch, tmp_path):
    """Callers that do not care -- the CLI -- keep the old behaviour."""
    monkeypatch.setattr(sample_audio.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(sample_audio, "monitor_source", lambda sink="": "x.monitor")
    monkeypatch.setattr(sample_audio, "POLL_SECONDS", 0.0)
    dest = tmp_path / "s.wav"
    monkeypatch.setattr(sample_audio.subprocess, "Popen",
                        lambda cmd, **kw: _FakePopen(
                            cmd, alive_polls=2,
                            on_start=lambda: dest.write_bytes(b"RIFFfake")))
    assert sample_audio.capture(45.0, dest=dest).path == dest


def test_ffmpeg_is_never_left_running(monkeypatch, tmp_path):
    """An orphaned ffmpeg on the monitor sink has happened before: one ran
    unparented for nearly two hours."""
    monkeypatch.setattr(sample_audio.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(sample_audio, "monitor_source", lambda sink="": "x.monitor")
    monkeypatch.setattr(sample_audio, "POLL_SECONDS", 0.0)
    proc = {}

    def fake_popen(cmd, **kw):
        proc["p"] = _FakePopen(cmd, alive_polls=10**6)   # never finishes
        return proc["p"]

    monkeypatch.setattr(sample_audio.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sample_audio.time, "monotonic",
                        _clock := (lambda counter=iter(range(0, 10**6, 40)):
                                   next(counter)))
    with pytest.raises(sample_audio.CaptureError, match="timed out"):
        sample_audio.capture(30.0, dest=tmp_path / "s.wav")
    assert proc["p"].killed
