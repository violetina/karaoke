"""Tests for playerctl/MPRIS integration in the CLI."""
from unittest.mock import MagicMock

import karaoke.cli as cli
from karaoke.identify import SongRef


def _args(player=True):
    return MagicMock(
        player=player, file=None, youtube=None, spotify=None,
        listen=None, output=None, query=None,
    )


def test_player_flag_resolves_song(monkeypatch):
    monkeypatch.setattr(
        "karaoke.playerctl.current_songref",
        lambda: SongRef(
            artist="The Cure", title="A Forest", album="Seventeen Seconds",
            url="https://www.youtube.com/watch?v=xik-y0xlpZ0", source="player",
        ),
    )

    ref = cli._resolve(_args())

    assert ref is not None
    assert ref.artist == "The Cure"
    assert ref.title == "A Forest"
    assert ref.album == "Seventeen Seconds"
    assert ref.url == "https://www.youtube.com/watch?v=xik-y0xlpZ0"
    assert ref.source == "player"


def test_player_flag_no_player(monkeypatch):
    monkeypatch.setattr("karaoke.playerctl.current_songref", lambda: None)
    assert cli._resolve(_args()) is None
