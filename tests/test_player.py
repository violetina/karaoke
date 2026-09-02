"""Tests for lyric timing logic and identify parsing (no audio/network)."""
import pytest
from karaoke.player import LyricTimeline, timeline_from_lyrics, render_lines, line_nudge_delta
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


def test_robust_offset_single():
    from karaoke.identify import robust_offset
    assert robust_offset([{"offset": 42.0}]) == 42.0


def test_robust_offset_rejects_outlier():
    from karaoke.identify import robust_offset
    # three tight ~89s + one 134.9s outlier (real songrec case) -> ~89
    ms = [{"offset": 89.14}, {"offset": 89.05}, {"offset": 89.41}, {"offset": 134.90}]
    r = robust_offset(ms)
    assert r is not None and 88.0 <= r <= 90.0


def test_robust_offset_empty():
    from karaoke.identify import robust_offset
    assert robust_offset([]) is None
    assert robust_offset([{"timeskew": 0.1}]) is None


def test_robust_offset_median_of_cluster():
    from karaoke.identify import robust_offset
    assert robust_offset([{"offset": 10.0}, {"offset": 12.0}, {"offset": 14.0}]) == 12.0


# --- live nudge (v=back / b=forward one line) ---------------------------------

NT = [10.0, 20.0, 30.0, 40.0]


def _apply(times, elapsed, direction):
    """Return the new elapsed after one nudge (delta added to the clock)."""
    return elapsed + line_nudge_delta(times, elapsed, direction)


def test_nudge_forward_advances_one_line():
    # At 22s the active line is index 1 (20s). Forward should land inside line 2 (30s).
    assert LyricTimeline([(t, "x") for t in NT]).active_index(_apply(NT, 22.0, +1)) == 2


def test_nudge_backward_steps_one_line():
    # At 22s (active index 1) backward should land inside line 0 (10s).
    e2 = _apply(NT, 22.0, -1)
    assert LyricTimeline([(t, "x") for t in NT]).active_index(e2) == 0


def test_nudge_forward_and_back_returns_close():
    # forward then back from the same spot should land on the original line again.
    tl = LyricTimeline([(t, "x") for t in NT])
    start = 22.0
    fwd = _apply(NT, start, +1)
    back = _apply(NT, fwd, -1)
    assert tl.active_index(back) == tl.active_index(start) == 1


def test_nudge_forward_at_last_line_is_noop():
    # Past the last line, forward can't advance further.
    assert line_nudge_delta(NT, 45.0, +1) == 0.0


def test_nudge_backward_in_intro_is_noop():
    # Before the first line, back can't go further.
    assert line_nudge_delta(NT, 2.0, -1) == 0.0


def test_nudge_backward_from_first_line_enters_intro():
    # At the first line, back should move into the intro (active index -1).
    e2 = _apply(NT, 12.0, -1)
    assert LyricTimeline([(t, "x") for t in NT]).active_index(e2) == -1


def test_nudge_empty_timeline():
    assert line_nudge_delta([], 5.0, +1) == 0.0
    assert line_nudge_delta([], 5.0, -1) == 0.0


# --- active_fraction (word-highlight interpolation) ---

def test_active_fraction_intro_is_zero():
    tl = LyricTimeline([(t, "x") for t in NT])
    assert tl.active_fraction(5.0) == 0.0


def test_active_fraction_midline():
    # line 0 spans 10..20; at 15s we're halfway.
    tl = LyricTimeline([(t, "x") for t in NT])
    assert tl.active_fraction(15.0) == pytest.approx(0.5)


def test_active_fraction_clamps_and_last_line_uses_tail():
    tl = LyricTimeline([(t, "x") for t in NT])
    # last line (40s) with default 4s tail: 42s -> 0.5
    assert tl.active_fraction(42.0, tail=4.0) == pytest.approx(0.5)
    # way past end clamps to 1.0
    assert tl.active_fraction(999.0) == 1.0


# --- instrumental-gap handling (issue #21) ---

def test_active_fraction_caps_line_duration_over_instrumental():
    """A long riff must not stretch a line's words across the whole gap.

    Regression for issue #21: 'Ren - Hi Ren' has 21.8s and 51.7s instrumental
    gaps; the highlight used to crawl one word every ~5s during them.
    """
    lines = [
        # A run of normally-paced lines establishes the song's rhythm...
        (0.0, "one two three four five six"),
        (3.0, "seven eight nine ten eleven twelve"),
        (6.0, "again some more words here now"),
        (9.0, "and yet more words follow here"),
        # ...then a line followed by a 51.7s instrumental break.
        (12.0, "When I was seventeen I shouted out into the void"),
        (63.7, "after the riff"),
    ]
    tl = LyricTimeline(lines)
    # The singer finishes this line in a few seconds, not 51.7s.
    assert tl.active_fraction(18.0) == 1.0
    # And it must not still be near the start well into the gap.
    assert tl.active_fraction(40.0) == 1.0


def test_active_fraction_unaffected_when_lines_are_close():
    """Densely-timed lyrics keep exact interpolation; the cap only clamps."""
    tl = LyricTimeline([(10.0, "a b c d"), (14.0, "next line here")])
    assert tl.active_fraction(12.0) == pytest.approx(0.5, abs=0.01)


def test_active_fraction_calibrates_to_slow_songs():
    """A slow ballad's own pacing sets the cap, so it is not cut short.

    NT lines are 1 word each spaced 10s apart: that IS this song's rhythm,
    so 15s must still read as halfway through line 0.
    """
    tl = LyricTimeline([(t, "x") for t in NT])
    assert tl.active_fraction(15.0) == pytest.approx(0.5)


def test_active_fraction_cap_is_overridable():
    tl = LyricTimeline([(0.0, "one two"), (100.0, "next")])
    assert tl.active_fraction(4.0, max_line_s=10.0) < 1.0
    assert tl.active_fraction(11.0, max_line_s=10.0) == 1.0


def test_line_cap_never_exceeds_hard_ceiling():
    """Even a pathologically slow song cannot hold one line forever."""
    lines = [(0.0, "a"), (600.0, "b")]
    tl = LyricTimeline(lines)
    assert tl.active_fraction(120.0) == 1.0


# --- in_gap (instrumental rest marker) ---

def _gap_timeline():
    return LyricTimeline([
        (0.0, "one two three four five six"),
        (3.0, "seven eight nine ten eleven twelve"),
        (6.0, "again some more words here now"),
        (9.0, "and yet more words follow here"),
        (12.0, "When I was seventeen I shouted out into the void"),
        (63.7, "after the riff"),
    ])


def test_in_gap_false_while_line_is_being_sung():
    assert _gap_timeline().in_gap(13.0) is False


def test_in_gap_true_during_instrumental_break():
    assert _gap_timeline().in_gap(40.0) is True


def test_in_gap_false_once_next_line_starts():
    assert _gap_timeline().in_gap(64.0) is False


def test_in_gap_false_for_short_pauses():
    """Ordinary line spacing must not flicker the rest marker."""
    tl = LyricTimeline([(0.0, "a b c"), (4.0, "d e f"), (8.0, "g h i")])
    assert tl.in_gap(3.5) is False


def test_in_gap_false_in_intro():
    assert _gap_timeline().in_gap(-1.0) is False


def test_in_gap_false_after_last_line():
    tl = LyricTimeline([(0.0, "only line")])
    assert tl.in_gap(500.0) is False


def test_gap_progress_runs_zero_to_one():
    tl = _gap_timeline()
    assert tl.gap_progress(13.0) == 0.0          # not in a gap yet
    mid = tl.gap_progress(40.0)
    assert 0.0 < mid < 1.0
    assert tl.gap_progress(63.6) == pytest.approx(1.0, abs=0.05)


def test_gap_marker_shows_note_and_progress():
    from karaoke.player import _gap_marker

    assert _gap_marker(0.0).startswith("♪ ")
    early, late = _gap_marker(0.1), _gap_marker(0.9)
    # The bar fills as the break elapses.
    assert early.index("•") < late.index("•")


def test_render_body_marks_gap_instead_of_stale_highlight():
    """During a riff the finished line must not stay actively highlighted."""
    from rich.text import Text
    from karaoke.player import _render_body

    tl = _gap_timeline()
    body = Text()
    _render_body(body, tl, 40.0)
    assert "♪" in body.plain


# --- real word timings (Enhanced LRC / captions) ---

def test_word_index_at_uses_real_timings():
    from karaoke.player import word_index_at

    times = [10.0, 10.5, 11.2, 12.0]
    assert word_index_at(times, 9.0) == 0     # before the first word
    assert word_index_at(times, 10.1) == 0
    assert word_index_at(times, 10.6) == 1
    assert word_index_at(times, 11.9) == 2
    assert word_index_at(times, 99.0) == 3    # clamps to last


def test_word_index_at_empty_returns_minus_one():
    from karaoke.player import word_index_at

    assert word_index_at([], 5.0) == -1


def test_timeline_word_index_prefers_real_timings_over_interpolation():
    """With real timings the highlight must not depend on line fraction."""
    tl = LyricTimeline(
        [(10.0, "one two three four"), (30.0, "next")],
        word_times={0: [10.0, 10.4, 10.8, 11.2]},
    )
    # Interpolation across 10..30s would still be on word 0 at 11.3s;
    # real timings put us on the last word.
    assert tl.word_index(11.3) == 3


def test_timeline_word_index_falls_back_to_interpolation():
    tl = LyricTimeline([(10.0, "one two three four"), (14.0, "next")])
    assert tl.word_index(13.9) == 3


def test_timeline_word_index_intro_returns_minus_one():
    tl = LyricTimeline([(10.0, "a b")])
    assert tl.word_index(1.0) == -1






# --- active_word_index (per-word purple highlight) ---

def test_active_word_index_spreads_words():
    from karaoke.player import active_word_index
    line = "one two three four"
    assert active_word_index(line, 0.0) == 0
    assert active_word_index(line, 0.3) == 1
    assert active_word_index(line, 0.6) == 2
    assert active_word_index(line, 0.99) == 3


def test_active_word_index_clamps_last():
    from karaoke.player import active_word_index
    assert active_word_index("a b c", 1.0) == 2
    assert active_word_index("a b c", 5.0) == 2


def test_active_word_index_blank_line():
    from karaoke.player import active_word_index
    assert active_word_index("", 0.5) == -1
    assert active_word_index("   ", 0.5) == -1

