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


def test_capture_records_from_the_monitor(monkeypatch, tmp_path):
    """The recorded source must be the monitor, never a microphone."""
    monkeypatch.setattr(sample_audio.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(sample_audio, "monitor_source",
                        lambda sink="": "bluez_output.spk.monitor")
    seen = {}
    dest = tmp_path / "s.wav"

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        dest.write_bytes(b"RIFFfake")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(sample_audio.subprocess, "run", fake_run)
    sample = sample_audio.capture(30.0, dest=dest)

    assert sample.source == "bluez_output.spk.monitor"
    assert "bluez_output.spk.monitor" in seen["cmd"]
    assert "-f" in seen["cmd"] and "pulse" in seen["cmd"]
    assert sample.path == dest


def test_capture_errors_when_the_file_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(sample_audio.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(sample_audio, "monitor_source", lambda sink="": "x.monitor")
    dest = tmp_path / "s.wav"

    def fake_run(cmd, **kwargs):
        dest.write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(sample_audio.subprocess, "run", fake_run)
    with pytest.raises(sample_audio.CaptureError, match="silent"):
        sample_audio.capture(30.0, dest=dest)


def test_capture_surfaces_an_ffmpeg_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(sample_audio.shutil, "which", lambda n: "/usr/bin/ffmpeg")
    monkeypatch.setattr(sample_audio, "monitor_source", lambda sink="": "x.monitor")

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, "", "device busy")

    monkeypatch.setattr(sample_audio.subprocess, "run", fake_run)
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
        bpm = None

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
