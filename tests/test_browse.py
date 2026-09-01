"""Tests for the interactive song browser helpers."""
from __future__ import annotations

from karaoke import browse


class DummyProcess:
    pid = 4242


def test_open_song_url_spawns_xdg_open(monkeypatch, tmp_path):
    calls = []

    def fake_popen(args, stdout=None, stderr=None):
        calls.append((args, stdout, stderr))
        return DummyProcess()

    monkeypatch.setattr(browse, "OPEN_STDOUT_LOG", tmp_path / "xdg.stdout.log")
    monkeypatch.setattr(browse, "OPEN_STDERR_LOG", tmp_path / "xdg.stderr.log")
    monkeypatch.setattr(browse.subprocess, "Popen", fake_popen)

    pid = browse.open_song_url("https://www.youtube.com/watch?v=bXWHf2HH8jY", "youtube")

    assert pid == 4242
    assert calls[0][0] == ["xdg-open", "https://www.youtube.com/watch?v=bXWHf2HH8jY"]
    assert calls[0][1] is not None
    assert calls[0][2] is not None


def test_open_song_url_uses_playerctl_for_spotify(monkeypatch):
    calls = []

    class Completed:
        stdout = ""
        stderr = ""

    def fake_run(args, check=False, capture_output=False, text=False):
        calls.append((args, check, capture_output, text))
        return Completed()

    monkeypatch.setattr(browse.subprocess, "run", fake_run)

    pid = browse.open_song_url("spotify:track:123", "spotify")

    assert pid is None
    assert calls == [(["playerctl", "open", "spotify:track:123"], True, True, True)]
