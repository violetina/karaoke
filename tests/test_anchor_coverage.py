"""How much of a track the anchors actually cover.

Interpolation is only as good as the anchors it runs between, and nothing in
the output distinguishes a line placed on a heard word from one guessed between
two distant ones. GAUPA - Febersvan made the cost concrete: anchors in the
first 58s and after 337s of a 448-second track, and the eleven lines
interpolated across the middle came out up to 183 seconds wrong while the
anchored ends were within a second.

The gap cannot be closed by matching harder -- Whisper heard the vocal at the
right moment and got the words wrong, and no similarity threshold admits
"shellturn"/"shelter" (0.750) while rejecting "feel"/"fell" (0.750) or
"feel"/"feet" (0.750). So it has to be noticed instead.
"""
from __future__ import annotations

import pytest

from karaoke.lyric_align import anchor_coverage


def test_evenly_anchored_lines_cover_the_track():
    longest, fraction = anchor_coverage([10.0, 20.0, 30.0, 40.0], horizon=50.0)
    assert longest == pytest.approx(10.0)
    assert fraction == pytest.approx(0.2)


def test_a_drought_in_the_middle_is_the_longest_gap():
    """GAUPA's shape, in miniature."""
    longest, fraction = anchor_coverage(
        [10.0, 20.0, None, None, None, 300.0, 310.0], horizon=400.0)
    assert longest == pytest.approx(280.0)
    assert fraction == pytest.approx(0.7)


def test_a_long_run_in_counts_as_uncovered():
    """Anchored only from the middle onward is still poorly covered."""
    longest, fraction = anchor_coverage([200.0, 210.0, 220.0], horizon=240.0)
    assert longest == pytest.approx(200.0)
    assert fraction == pytest.approx(200.0 / 240.0)


def test_a_long_run_out_counts_as_uncovered():
    """Anchored only in the opening is the mirror image, and just as weak."""
    longest, fraction = anchor_coverage([5.0, 10.0, 15.0], horizon=300.0)
    assert longest == pytest.approx(285.0)
    assert fraction == pytest.approx(285.0 / 300.0)


def test_no_anchors_at_all_is_wholly_uncovered():
    longest, fraction = anchor_coverage([None, None, None], horizon=200.0)
    assert longest == pytest.approx(200.0)
    assert fraction == 1.0


def test_no_anchors_and_no_horizon_does_not_divide_by_zero():
    assert anchor_coverage([None], horizon=None) == (0.0, 1.0)


def test_a_missing_horizon_falls_back_to_the_last_anchor():
    longest, fraction = anchor_coverage([10.0, 100.0], horizon=None)
    assert longest == pytest.approx(90.0)
    assert fraction == pytest.approx(0.9)


def test_a_zero_horizon_falls_back_to_the_last_anchor():
    """Zero is treated as unknown, not as a track of no length."""
    assert anchor_coverage([10.0], horizon=0.0) == (10.0, 1.0)


def test_an_anchor_at_zero_with_no_horizon_reports_nothing():
    """The only guard against dividing by a span of nothing."""
    assert anchor_coverage([0.0], horizon=None) == (0.0, 0.0)


def test_the_fraction_never_exceeds_one():
    """An anchor past the horizon must not report 130% uncovered."""
    _longest, fraction = anchor_coverage([500.0], horizon=100.0)
    assert fraction <= 1.0


def test_a_single_anchor_at_the_start_of_a_long_track_is_a_drought():
    longest, fraction = anchor_coverage([2.0], horizon=400.0)
    assert longest == pytest.approx(398.0)
    assert fraction > 0.99


# --- the metric reaches callers through align_lines -------------------------

def test_align_lines_reports_its_own_support():
    from karaoke.lyric_align import align_lines
    from karaoke.whisper_sync import Word

    words = [Word(start=1.0, end=1.4, text="hello", probability=0.9),
             Word(start=2.0, end=2.4, text="world", probability=0.9)]
    report: dict = {}
    align_lines(["hello", "world"], words, total_duration=10.0, report=report)

    assert report["lines"] == 2
    assert report["anchored"] == 2
    assert "longest_gap_s" in report
    assert "unanchored_fraction" in report


def test_align_lines_reports_a_drought_when_most_lines_are_guessed():
    """GAUPA's shape: anchors at both ends, nothing heard in between.

    The words must span the track, because the horizon is pulled back to the
    last heard word -- otherwise this measures a short sung span rather than a
    drought inside a long one.
    """
    from karaoke.lyric_align import align_lines
    from karaoke.whisper_sync import Word

    words = [Word(start=1.0, end=1.4, text="hello", probability=0.9),
             Word(start=590.0, end=590.4, text="world", probability=0.9)]
    report: dict = {}
    align_lines(["hello", "unheard one", "unheard two", "unheard three",
                 "unheard four", "world"],
                words, total_duration=600.0, report=report)

    assert report["anchored"] < report["lines"]
    assert report["longest_gap_s"] > 500.0
    assert report["unanchored_fraction"] > 0.9


def test_a_sung_span_far_shorter_than_the_track_is_reported_separately():
    """Six lines crammed into whatever fragment Whisper heard.

    A different pathology from a drought, and invisible if only coverage
    within the sung span is reported: that span is well anchored, it is simply
    almost none of the song.
    """
    from karaoke.lyric_align import align_lines
    from karaoke.whisper_sync import Word

    words = [Word(start=1.0, end=1.4, text="hello", probability=0.9),
             Word(start=2.0, end=2.4, text="world", probability=0.9)]
    report: dict = {}
    align_lines(["hello", "world", "unheard one", "unheard two"],
                words, total_duration=600.0, report=report)

    assert report["horizon_s"] < 5.0
    assert report["total_duration_s"] == 600.0


def test_the_report_is_optional_and_costs_callers_nothing():
    from karaoke.lyric_align import align_lines
    from karaoke.whisper_sync import Word

    words = [Word(start=1.0, end=1.4, text="hello", probability=0.9)]
    assert align_lines(["hello"], words, total_duration=10.0)


def test_a_transcription_with_no_usable_words_still_reports():
    """The early-return path fills the report too.

    Leaving it empty let this case slip past both the support flag and the
    caller's zero-anchor refusal: "Jimi Hendrix - Sweet Angel" was stored as
    980 characters of whisper_aligned timings with no anchor behind any line
    and nothing recorded to say so. Found because a reporting script crashed
    formatting a None, not because anything checked.
    """
    from karaoke.lyric_align import align_lines

    report: dict = {}
    placed = align_lines(["first line", "second line"], [],
                         total_duration=200.0, report=report)

    assert len(placed) == 2                  # still timed, by weight alone
    assert report["lines"] == 2
    assert report["anchored"] == 0           # which is what must be visible
    assert report["unanchored_fraction"] == 1.0


def test_no_lyric_lines_needs_no_report():
    """Nothing to describe, and the caller has nothing to decide."""
    from karaoke.lyric_align import align_lines

    report: dict = {}
    assert align_lines([], [], total_duration=10.0, report=report) == []
    assert report == {}
