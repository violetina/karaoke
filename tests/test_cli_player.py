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


# --- YouTube -> YouTube Music routing ---------------------------------------

def _opened(url, kind, monkeypatch):
    from karaoke import player_open as po
    seen = []
    monkeypatch.setattr(po, "try_chrome_cdp_navigate",
                        lambda u: seen.append(u) or True)
    monkeypatch.setattr(po.subprocess, "run", lambda *a, **k: None)
    po.open_song_url(url, kind)
    return seen[0]


def test_watch_urls_open_in_youtube_music(monkeypatch):
    # A real 11-character video id; extract_youtube_id rejects shorter ones.
    vid = "_3tkup9b-iM"
    want = f"https://music.youtube.com/watch?v={vid}"
    assert _opened(f"https://www.youtube.com/watch?v={vid}", "youtube",
                   monkeypatch) == want
    assert _opened(f"https://youtu.be/{vid}", "youtube", monkeypatch) == want


def test_search_urls_also_open_in_youtube_music(monkeypatch):
    """A search has no video id, but the query carries over."""
    out = _opened("https://www.youtube.com/results?search_query=tom+waits",
                  "youtube_search", monkeypatch)
    assert out == "https://music.youtube.com/search?q=tom+waits"


def test_spotify_urls_are_untouched_by_the_youtube_rewrite(monkeypatch):
    out = _opened("https://open.spotify.com/track/abc", "spotify", monkeypatch)
    assert "music.youtube.com" not in out
