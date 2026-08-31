"""Tests for playerctl/MPRIS metadata cleanup."""
import subprocess
from unittest.mock import MagicMock

from karaoke import playerctl


def test_normalize_player_track_strips_duplicate_artist_from_title():
    ref = playerctl.normalize_player_track("Tom Waits", 'Tom Waits - "Watch Her Disappear"')
    assert ref.artist == "Tom Waits"
    assert ref.title == "Watch Her Disappear"
    assert ref.source == "player"


def test_normalize_player_track_parses_artist_title_when_artist_missing():
    ref = playerctl.normalize_player_track("", "The Cure - A Forest")
    assert ref.artist == "The Cure"
    assert ref.title == "A Forest"


def test_current_songref_resolves_metadata(monkeypatch):
    result = MagicMock()
    result.stdout = "The Cure\x1fA Forest\x1fSeventeen Seconds\x1f\x1fvlc\n"
    mock_run = MagicMock(return_value=result)
    monkeypatch.setattr(subprocess, "run", mock_run)

    ref = playerctl.current_songref()

    assert ref is not None
    assert ref.artist == "The Cure"
    assert ref.title == "A Forest"
    assert ref.album == "Seventeen Seconds"
    mock_run.assert_called_once_with(
        ["playerctl", "metadata", "--format", "{{artist}}\x1f{{title}}\x1f{{album}}\x1f{{xesam:url}}\x1f{{playerName}}"],
        capture_output=True, text=True, timeout=5, check=True,
    )


def test_current_songref_returns_none_when_playerctl_fails(monkeypatch):
    mock_run = MagicMock(side_effect=FileNotFoundError)
    monkeypatch.setattr(subprocess, "run", mock_run)
    assert playerctl.current_songref() is None
