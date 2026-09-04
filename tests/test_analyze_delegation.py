"""Analysis falls back to the isolated audio venv.

The DSP stack lives in .venv-audio on purpose (`make install-audio`), but
analyze_audio imports it in-process -- so every venv that wanted to analyse
needed a duplicate copy, and a worktree whose venv lacked it failed at the
point of use with nothing pointing at the cause. This bit three times before
it was fixed properly.
"""
import json
import subprocess
from pathlib import Path

import pytest

from karaoke import analyze


def test_audio_python_honours_the_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "python"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("KARAOKE_AUDIO_PYTHON", str(fake))
    assert analyze.audio_python() == str(fake)


def test_a_bogus_override_is_ignored(monkeypatch):
    """Better to fall through to the in-process path than exec nothing."""
    monkeypatch.setenv("KARAOKE_AUDIO_PYTHON", "/nonexistent/python")
    assert analyze.audio_python() is None


def test_the_audio_venv_is_found_per_worktree(monkeypatch):
    """Each worktree has its own .venv-audio; the wrong one would be worse
    than none, since it would analyse against different code."""
    monkeypatch.delenv("KARAOKE_AUDIO_PYTHON", raising=False)
    found = analyze.audio_python()
    if found is not None:
        root = Path(analyze.__file__).resolve().parents[2]
        assert Path(found).is_relative_to(root)


def test_delegation_rebuilds_the_analysis(monkeypatch):
    """The numbers must survive the trip out and back."""
    payload = {
        "key": "D minor", "key_confidence": 0.66, "key_agreement": "4/6",
        "bpm": 129.2, "method": "essentia-edma-vote",
        "energy": 0.32, "brightness": 0.22, "version": 1,
    }

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(analyze.subprocess, "run", fake_run)
    result = analyze._analyze_out_of_process("/tmp/x.wav", "/usr/bin/python")
    assert result.key.name == "D minor"
    assert result.bpm == 129.2
    assert result.key_agreement == "4/6"
    assert result.energy == 0.32


def test_delegation_survives_a_chatty_subprocess(monkeypatch):
    """essentia prints banners; only the last line is the payload."""
    noisy = "[ INFO ] MusicExtractorSVM: no models\n" + json.dumps(
        {"key": None, "bpm": 100.0, "method": "x"})

    monkeypatch.setattr(analyze.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, noisy, ""))
    result = analyze._analyze_out_of_process("/tmp/x.wav", "/usr/bin/python")
    assert result is not None and result.bpm == 100.0


def test_delegation_returns_none_on_a_failure(monkeypatch):
    monkeypatch.setattr(analyze.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom"))
    assert analyze._analyze_out_of_process("/tmp/x.wav", "/usr/bin/python") is None


def test_delegation_returns_none_on_unparseable_output(monkeypatch):
    monkeypatch.setattr(analyze.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "not json", ""))
    assert analyze._analyze_out_of_process("/tmp/x.wav", "/usr/bin/python") is None


def test_delegation_returns_none_when_the_interpreter_is_missing(monkeypatch):
    def boom(cmd, **kw):
        raise OSError("no such file")

    monkeypatch.setattr(analyze.subprocess, "run", boom)
    assert analyze._analyze_out_of_process("/tmp/x.wav", "/nope") is None


def test_an_available_stack_is_not_delegated(monkeypatch):
    """The in-process path stays the fast one; no subprocess for nothing."""
    monkeypatch.setattr(analyze, "stack_available", lambda: True)
    monkeypatch.setattr(analyze, "audio_python",
                        lambda: pytest.fail("must not delegate when local"))
    analyze.analyze_audio("/nonexistent.wav")


def test_no_audio_venv_still_degrades_rather_than_raising(monkeypatch):
    """The original contract: unavailable, not an exception."""
    monkeypatch.setattr(analyze, "stack_available", lambda: False)
    monkeypatch.setattr(analyze, "audio_python", lambda: None)
    result = analyze.analyze_audio("/nonexistent.wav")
    assert result.method == "unavailable"
    assert result.key is None
