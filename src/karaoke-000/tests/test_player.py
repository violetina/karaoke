"""Tests for lyric timing logic and identify parsing (no audio/network)."""
from karaoke.player import LyricTimeline, timeline_from_lyrics, render_lines
from karaoke.lyrics import Lyrics
from karaoke.identify import parse_query, SongRef


LINES = [(10.0, "line one"), (20.0, "line two"), (30.0, "line three")]


def test_active_index_before_first_is_intro():
    tl = LyricTimeline(LINES)
    assert tl.active_index(0.0) == -1
    assert tl.active_index(9.99) == -1


def test_active_index_on_and_after_boundaries():
    tl = LyricTimeline(LINES)
    assert tl.active_index(10.0) == 0
    assert tl.active_index(15.0) == 0
    assert tl.active_index(20.0) == 1
    assert tl.active_index(29.999) == 1
    assert tl.active_index(30.0) == 2
    assert tl.active_index(999.0) == 2  # stays on last


def test_next_time():
    tl = LyricTimeline(LINES)
    assert tl.next_time(0.0) == 10.0
    assert tl.next_time(10.0) == 20.0
    assert tl.next_time(25.0) == 30.0
    assert tl.next_time(30.0) is None


def test_empty_timeline():
    tl = LyricTimeline([])
    assert tl.active_index(5.0) == -1
    assert tl.next_time(5.0) is None
    assert render_lines(tl, -1) == "(no synced lyrics)"


def test_timeline_from_lyrics_uses_lines():
    ly = Lyrics(lines=LINES, source="lrclib")
    tl = timeline_from_lyrics(ly)
    assert tl.lines == LINES


def test_timeline_from_lyrics_parses_raw_when_no_lines():
    ly = Lyrics(synced_raw="[00:05.00] hi\n[00:07.50] bye", source="lrclib")
    tl = timeline_from_lyrics(ly)
    assert tl.lines == [(5.0, "hi"), (7.5, "bye")]


def test_render_window_marks_active():
    tl = LyricTimeline(LINES)
    out = render_lines(tl, 1, context=1)
    assert ">> line two" in out
    assert "line one" in out and "line three" in out


def test_parse_query_artist_title():
    ref = parse_query("R.E.M. - Losing My Religion")
    assert isinstance(ref, SongRef)
    assert ref.artist == "R.E.M."
    assert ref.title == "Losing My Religion"
    assert ref.source == "query"


def test_parse_query_title_only():
    ref = parse_query("Bohemian Rhapsody")
    assert ref.artist == ""
    assert ref.title == "Bohemian Rhapsody"
