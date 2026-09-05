"""Tests for playerctl/MPRIS metadata cleanup."""
import subprocess
from unittest.mock import MagicMock

import pytest

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
        ["playerctl", "metadata", "--format", playerctl._metadata_format()],
        capture_output=True, text=True, timeout=5, check=True,
    )


def test_current_songref_returns_none_when_playerctl_fails(monkeypatch):
    mock_run = MagicMock(side_effect=FileNotFoundError)
    monkeypatch.setattr(subprocess, "run", mock_run)
    assert playerctl.current_songref() is None


# --- player selection ------------------------------------------------------
#
# Bare `playerctl metadata` answers from whichever player playerctl picks first,
# ignoring playback status. With the kiosk browser left open for CDP -- its
# normal resting state -- that was routinely a *paused* tab holding a stale
# track, which shadowed the player actually making sound.

class _FakePlayerctl:
    """Stands in for the playerctl binary; records what was asked."""

    def __init__(self, players, statuses):
        self.players = players
        self.statuses = statuses
        self.status_calls = []

    def run(self, cmd, **kwargs):
        if cmd[-1] == "--list-all":
            return "\n".join(self.players)
        if cmd[-1] == "status":
            player = cmd[cmd.index("--player") + 1] if "--player" in cmd else ""
            self.status_calls.append(player)
            return self.statuses.get(player, "Stopped")
        return None


def _patch(monkeypatch, fake):
    from karaoke import playerctl
    monkeypatch.setattr(playerctl, "_run", lambda cmd, **kw: fake.run(cmd, **kw))
    return fake


def test_playing_player_skips_the_paused_one(monkeypatch):
    """The reported bug: paused kiosk Chrome shadowed a playing Spotify."""
    from karaoke import playerctl

    fake = _patch(monkeypatch, _FakePlayerctl(
        ["chromium.instance402904", "spotify"],
        {"chromium.instance402904": "Paused", "spotify": "Playing"},
    ))
    assert playerctl.playing_player() == "spotify"
    assert playerctl.playing_players() == ["spotify"]


def test_no_status_probing_with_a_single_player(monkeypatch):
    """Detection runs on a 1.5s timer; the common case must stay cheap."""
    from karaoke import playerctl

    fake = _patch(monkeypatch, _FakePlayerctl(["spotify"], {"spotify": "Playing"}))
    assert playerctl.playing_players() == ["spotify"]
    assert fake.status_calls == []


def test_nothing_playing_yields_no_player(monkeypatch):
    from karaoke import playerctl

    _patch(monkeypatch, _FakePlayerctl(
        ["a", "b"], {"a": "Paused", "b": "Stopped"}))
    assert playerctl.playing_player() == ""


def test_several_playing_are_all_returned(monkeypatch):
    from karaoke import playerctl

    _patch(monkeypatch, _FakePlayerctl(
        ["chromium.instance1", "spotify"],
        {"chromium.instance1": "Playing", "spotify": "Playing"},
    ))
    assert playerctl.playing_players() == ["chromium.instance1", "spotify"]


def test_missing_playerctl_is_not_an_error(monkeypatch):
    from karaoke import playerctl

    monkeypatch.setattr(playerctl, "_run", lambda cmd, **kw: None)
    assert playerctl.list_players() == []
    assert playerctl.playing_player() == ""


# --- mpris:length -----------------------------------------------------------
#
# Duration was never requested from MPRIS at all, so the deduplicator's
# duration guard -- the thing meant to keep a demo out of the studio cut -- had
# nothing to work with and abstained on every pair.

def test_length_is_converted_from_microseconds():
    """The MPRIS spec reports it in microseconds."""
    assert playerctl._length_seconds("205533333") == pytest.approx(205.533333)


def test_a_missing_length_is_unknown():
    assert playerctl._length_seconds("") is None
    assert playerctl._length_seconds("n/a") is None


def test_a_zero_length_is_unknown_not_zero():
    """Zero means the player does not know; treating it as a real duration
    would make every such track look absurdly short."""
    assert playerctl._length_seconds("0") is None


def test_metadata_requests_the_length():
    assert "mpris:length" in playerctl._metadata_format()


def test_metadata_parses_the_length(monkeypatch):
    from unittest.mock import MagicMock

    result = MagicMock()
    result.stdout = ("The Cure\x1fA Forest\x1fSeventeen Seconds\x1f\x1fvlc"
                     "\x1f205533333\n")
    monkeypatch.setattr(subprocess, "run", MagicMock(return_value=result))
    meta = playerctl.current_metadata()
    assert meta is not None
    assert meta.duration == pytest.approx(205.533333)
    assert meta.album == "Seventeen Seconds"


def test_metadata_survives_a_player_that_omits_the_length(monkeypatch):
    """Older players send fewer fields; the parse must not fall over."""
    from unittest.mock import MagicMock

    result = MagicMock()
    result.stdout = "The Cure\x1fA Forest\x1fSeventeen Seconds\x1f\x1fvlc\n"
    monkeypatch.setattr(subprocess, "run", MagicMock(return_value=result))
    meta = playerctl.current_metadata()
    assert meta is not None and meta.duration is None


def test_pause_is_not_a_toggle():
    """A caller that wants silence must not start playback because the track
    happened to be paused already."""
    from unittest.mock import MagicMock

    calls = []
    monkey = MagicMock(side_effect=lambda cmd, **kw: calls.append(cmd) or
                       MagicMock(stdout="", returncode=0))
    import karaoke.playerctl as pc
    original = pc._run
    pc._run = lambda cmd, **kw: calls.append(cmd) or ""
    try:
        pc.pause("spotify")
    finally:
        pc._run = original
    assert calls and calls[-1][-1] == "pause"
