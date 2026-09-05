"""What the TUI can show for a track: synced, unsynced, or nothing.

Three states, not two. Treating this as "synced or nothing" made the TUI report
"No synced lyrics — added to the staging/backfill queue" for The Sound's
"Skeletons" while 329 characters of real LRCLIB words sat in the database and
the player showed those same words on screen. It reported an absence that was
not there and queued work that was not needed.

Eleven tracks in the library are in that state, including three Wizards of Ooze
transcriptions.
"""
from __future__ import annotations

import pytest

from karaoke.lyrics import Lyrics, parse_lrc
from karaoke.tui import lyric_display_state

# has_synced is driven by the *parsed* lines, not by the raw LRC text, so a
# Lyrics built without them is unsynced however much LRC it carries. Built
# here the way the fetchers and the cache build it.
SYNCED_RAW = "[00:10.00]first line\n[00:14.00]second line"


def _synced(**over) -> Lyrics:
    fields = {"synced_raw": SYNCED_RAW, "lines": parse_lrc(SYNCED_RAW),
              "source": "lrclib"}
    fields.update(over)
    return Lyrics(**fields)


def test_synced_lyrics_are_synced():
    assert lyric_display_state(
        _synced()) == "synced"


def test_words_without_timings_are_unsynced():
    """The case that was being reported as "no lyrics"."""
    assert lyric_display_state(
        Lyrics(plain="There's a gaping hole in the way we are",
               source="lrclib")) == "unsynced"


def test_nothing_at_all_is_none():
    assert lyric_display_state(Lyrics(source="")) == "none"


def test_a_missing_lyrics_object_is_none():
    assert lyric_display_state(None) == "none"


def test_whitespace_is_not_words():
    """An empty row must not render a blank panel as though it held lyrics."""
    assert lyric_display_state(Lyrics(plain="   \n\n  ")) == "none"


def test_synced_wins_when_both_are_present():
    """Timings are what the display is for; plain is the fallback."""
    assert lyric_display_state(
        _synced(plain="first line\nsecond line")) == "synced"


def test_a_whisper_transcription_without_timings_still_shows():
    """Three Wizards of Ooze tracks are exactly this, and had shown nothing."""
    assert lyric_display_state(
        Lyrics(plain="bambee bambee", source="whisper")) == "unsynced"


def test_the_state_is_one_of_exactly_three_values():
    for lyrics in (None, Lyrics(source=""), Lyrics(plain="x"), _synced()):
        assert lyric_display_state(lyrics) in {"synced", "unsynced", "none"}
