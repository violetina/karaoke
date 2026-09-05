"""Reading the lyrics YouTube Music is already showing.

A track can play with its full lyrics on screen -- the SONGTEKST tab, sourced
from LyricFind -- while the library reports "no lyrics". LRCLIB does not have
every song, and YouTube *captions* are a different thing (a video's subtitle
track, not the lyrics panel), so nothing ever looked at what was in front of
the user.
"""
import json

import pytest

from karaoke import ytmusic_lyrics as yl


def _panel(text, attribution="Bron: LyricFind"):
    return yl.PanelLyrics(text=text, attribution=attribution, url="http://x")


LYRICS = "Someone walked away\nSomeone walked away\nSomeone walked away from me\n\nI tried to tell you"


# -- reading the panel --------------------------------------------------

def test_a_populated_panel_is_read(monkeypatch):
    payload = {"present": True, "text": LYRICS,
               "attribution": "Bron: LyricFind", "url": "http://x"}
    monkeypatch.setattr("karaoke.player_open._cdp_send",
                        lambda *a, **k: {"result": {"result": {"value": json.dumps(payload)}}})
    panel = yl.read_panel()
    assert panel is not None
    assert len(panel.lines) == 4          # the blank line is not a lyric
    assert panel.attribution == "Bron: LyricFind"


def test_an_absent_panel_is_none(monkeypatch):
    monkeypatch.setattr("karaoke.player_open._cdp_send",
                        lambda *a, **k: {"result": {"result": {"value": '{"present": false}'}}})
    assert yl.read_panel() is None


def test_an_empty_panel_is_none(monkeypatch):
    """The tab exists but is still loading, or the track has no lyrics."""
    payload = {"present": True, "text": "   ", "attribution": ""}
    monkeypatch.setattr("karaoke.player_open._cdp_send",
                        lambda *a, **k: {"result": {"result": {"value": json.dumps(payload)}}})
    assert yl.read_panel() is None


def test_no_browser_is_not_an_error(monkeypatch):
    monkeypatch.setattr("karaoke.player_open._cdp_send", lambda *a, **k: None)
    assert yl.read_panel() is None


def test_unparseable_output_is_none(monkeypatch):
    monkeypatch.setattr("karaoke.player_open._cdp_send",
                        lambda *a, **k: {"result": {"result": {"value": "not json"}}})
    assert yl.read_panel() is None


# -- judging what came back ---------------------------------------------

def test_a_short_panel_is_not_usable():
    """A placeholder or an error message is not lyrics."""
    assert not yl.usable(_panel("Lyrics unavailable"))
    assert not yl.usable(None)


def test_a_full_panel_is_usable():
    assert yl.usable(_panel(LYRICS))


def test_the_provider_is_named_in_the_source():
    """Names where the words came from, not how they were obtained -- the
    provider is what matters when judging them later."""
    assert yl.lyrics_source(_panel(LYRICS)) == "ytmusic_panel_lyricfind"


def test_an_english_attribution_works_too():
    assert yl.lyrics_source(_panel(LYRICS, "Source: Musixmatch")) == \
        "ytmusic_panel_musixmatch"


def test_an_unknown_provider_still_yields_a_source():
    assert yl.lyrics_source(_panel(LYRICS, "")) == "ytmusic_panel_unknown"


# -- it must be the track we think it is --------------------------------

def test_the_panel_must_match_the_playing_track(monkeypatch):
    """The panel lags a track change; attributing one song's words to another
    is invisible once stored."""
    from karaoke import playerctl

    monkeypatch.setattr(yl, "read_panel", lambda: _panel(LYRICS))
    monkeypatch.setattr(playerctl, "current_metadata",
                        lambda *a, **k: playerctl.PlayerMetadata(
                            artist="Dinosaur Jr.", title="Put It Down"))
    assert yl.for_playing("Dinosaur Jr.", "Put It Down") is not None
    assert yl.for_playing("Someone Else", "Wrong Song") is None


def test_no_player_means_no_attribution(monkeypatch):
    from karaoke import playerctl

    monkeypatch.setattr(yl, "read_panel", lambda: _panel(LYRICS))
    monkeypatch.setattr(playerctl, "current_metadata", lambda *a, **k: None)
    assert yl.for_playing("Dinosaur Jr.", "Put It Down") is None


def test_an_unusable_panel_is_never_attributed(monkeypatch):
    monkeypatch.setattr(yl, "read_panel", lambda: _panel("nope"))
    assert yl.for_playing("A", "B") is None


# -- the TUI fallback ---------------------------------------------------

def test_the_tui_stores_panel_lyrics_as_plain(monkeypatch):
    """LyricFind supplies words, not timings, so these are plain text and a
    candidate for alignment rather than a finished result."""
    from karaoke.tui import KaraokeTui

    monkeypatch.setattr(yl, "for_playing", lambda a, t: _panel(LYRICS))
    app = KaraokeTui.__new__(KaraokeTui)
    lyrics = app._panel_lyrics("Dinosaur Jr.", "Put It Down")
    assert lyrics is not None
    assert lyrics.source == "ytmusic_panel_lyricfind"
    assert lyrics.plain.startswith("Someone walked away")
    assert not lyrics.synced_raw          # nothing pretends to be timed


def test_the_tui_survives_a_missing_panel(monkeypatch):
    from karaoke.tui import KaraokeTui

    monkeypatch.setattr(yl, "for_playing", lambda a, t: None)
    app = KaraokeTui.__new__(KaraokeTui)
    assert app._panel_lyrics("A", "B") is None


def test_the_attribution_is_not_a_lyric():
    """It renders inside the same element as the words, so without stripping
    it becomes the last line of every song -- timed, stored and sung."""
    panel = _panel("first\nsecond\nthird\nfourth\nBron: LyricFind")
    assert "Bron: LyricFind" not in panel.lines
    assert panel.lines[-1] == "fourth"


def test_an_english_attribution_is_stripped_too():
    panel = _panel("a\nb\nc\nd\nSource: Musixmatch")
    assert len(panel.lines) == 4


def test_a_lyric_mentioning_a_source_survives():
    """Only a leading "Bron:"/"Source:" is an attribution."""
    panel = _panel("the source of all my trouble\nb\nc\nd")
    assert panel.lines[0] == "the source of all my trouble"
