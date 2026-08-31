"""Tests for playerctl/MPRIS integration in the CLI."""
import subprocess
from unittest.mock import MagicMock

import pytest

import karaoke.cli as cli


def test_player_flag_resolves_song(monkeypatch):
    mock_run = MagicMock()
    result = MagicMock()
    result.stdout = "The Cure - A Forest"
    result.stderr = ""
    result.returncode = 0
    mock_run.return_value = result
    monkeypatch.setattr(subprocess, "run", mock_run)

    ref = cli._resolve(MagicMock(player=True, file=None, youtube=None, spotify=None, listen=None, output=None, query=None))

    assert ref is not None
    assert ref.artist == "The Cure"
    assert ref.title == "A Forest"
    assert ref.source == "player"
    mock_run.assert_called_once_with(
        ["playerctl", "metadata", "--format", "{{artist}} - {{title}}"],
        capture_output=True, text=True, timeout=5, check=True
    )

def test_player_flag_no_player(monkeypatch):
    mock_run = MagicMock()
    error = subprocess.CalledProcessError(1, "playerctl")
    error.stdout = ""
    error.stderr = "No players found"
    mock_run.side_effect = error
    monkeypatch.setattr(subprocess, "run", mock_run)

    ref = cli._resolve(MagicMock(player=True, file=None, youtube=None, spotify=None, listen=None, output=None, query=None))
    assert ref is None

def test_player_flag_no_metadata(monkeypatch):
    mock_run = MagicMock()
    result = MagicMock()
    result.stdout = ""
    result.stderr = ""
    result.returncode = 0
    mock_run.return_value = result
    monkeypatch.setattr(subprocess, "run", mock_run)

    ref = cli._resolve(MagicMock(player=True, file=None, youtube=None, spotify=None, listen=None, output=None, query=None))
    assert ref is None

def test_player_flag_playerctl_not_found(monkeypatch):
    mock_run = MagicMock()
    mock_run.side_effect = FileNotFoundError
    monkeypatch.setattr(subprocess, "run", mock_run)

    ref = cli._resolve(MagicMock(player=True, file=None, youtube=None, spotify=None, listen=None, output=None, query=None))
    assert ref is None
