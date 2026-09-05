"""Scoring an alignment against known-good timings.

The point of these is that the score must separate two different errors. The
segment boundary comes from songrec offsets and is only accurate to a few
seconds; if it is 2 s late then every line is 2 s late, and reporting that as
alignment error would blame the aligner for something it did not do.
"""
import importlib.util
from pathlib import Path

import pytest

# eval_align lives in scripts/, not the karaoke package; load it directly.
_SPEC = importlib.util.spec_from_file_location(
    "eval_align",
    Path(__file__).resolve().parent.parent / "scripts" / "eval_align.py",
)
eval_align = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(eval_align)


TRUTH = [(10.0, "first line"), (14.0, "second line"),
         (18.0, "third line"), (22.0, "fourth line")]


def test_a_perfect_alignment_scores_zero():
    stats = eval_align.score(list(TRUTH), TRUTH, horizon=60.0)
    assert stats["median_abs"] == pytest.approx(0.0)
    assert stats["median_jitter"] == pytest.approx(0.0)
    assert stats["within"] == 1.0


def test_a_constant_shift_is_reported_as_systematic_not_as_jitter():
    """The whole point: a late segment boundary is not an aligner error."""
    shifted = [(t + 2.0, text) for t, text in TRUTH]
    stats = eval_align.score(shifted, TRUTH, horizon=60.0)

    assert stats["median_abs"] == pytest.approx(2.0)
    assert stats["median_signed"] == pytest.approx(2.0)
    # Removing the systematic part leaves nothing: the aligner was perfect.
    assert stats["median_jitter"] == pytest.approx(0.0)
    assert stats["within"] == 0.0            # every line reads as late...
    assert stats["within_detrended"] == 1.0  # ...but the spacing is exact


def test_the_sign_of_a_shift_is_kept():
    """Early and late are different problems; early is usually a bad anchor."""
    early = [(t - 1.5, text) for t, text in TRUTH]
    stats = eval_align.score(early, TRUTH, horizon=60.0)
    assert stats["median_signed"] == pytest.approx(-1.5)


def test_scatter_shows_up_as_jitter():
    scattered = [(10.0, "a"), (15.0, "b"), (17.0, "c"), (25.0, "d")]
    stats = eval_align.score(scattered, TRUTH, horizon=60.0)
    assert stats["median_jitter"] > 0.5
    assert stats["median_signed"] == pytest.approx(0.5)


def test_one_badly_late_line_moves_p90_but_not_the_median():
    """A line eight seconds out matters more than ten lines slightly off."""
    produced = [(10.0, "a"), (14.0, "b"), (18.0, "c"), (30.0, "d")]
    stats = eval_align.score(produced, TRUTH, horizon=60.0)
    assert stats["median_abs"] == pytest.approx(0.0)
    assert stats["p90_abs"] == pytest.approx(8.0)


def test_lines_past_the_audible_span_are_not_scored():
    """A skipped track played in part; it cannot be judged on what never sang."""
    produced = [(10.0, "a"), (14.0, "b")]
    stats = eval_align.score(produced, TRUTH, horizon=16.0)
    assert stats["scored_of"] == 2      # only two true lines fall inside
    assert stats["truth_lines"] == 4    # but the full count is still reported
    assert stats["lines"] == 2
    assert stats["median_abs"] == pytest.approx(0.0)


def test_a_capture_that_stops_before_any_line_scores_nothing():
    assert eval_align.score([(1.0, "a")], TRUTH, horizon=5.0) is None


def test_no_produced_lines_scores_nothing():
    assert eval_align.score([], TRUTH, horizon=60.0) is None


def test_extra_produced_lines_are_ignored_rather_than_penalised():
    """Pairing is positional, and the truth inside the span is the yardstick."""
    produced = [(10.0, "a"), (14.0, "b"), (18.0, "c"), (22.0, "d"),
                (26.0, "e"), (30.0, "f")]
    stats = eval_align.score(produced, TRUTH, horizon=60.0)
    assert stats["lines"] == 4
    assert stats["median_abs"] == pytest.approx(0.0)


def test_the_noticeable_threshold_is_a_quarter_of_a_second_ish():
    """Guarding the constant: it is what "within" means to a reader."""
    assert 0.2 <= eval_align.NOTICEABLE_S <= 0.5


def test_within_counts_only_lines_a_singer_would_accept():
    produced = [(10.1, "a"), (14.2, "b"), (18.9, "c"), (24.0, "d")]
    stats = eval_align.score(produced, TRUTH, horizon=60.0)
    # 0.1 and 0.2 are inside 0.3; 0.9 and 2.0 are not.
    assert stats["within"] == pytest.approx(0.5)


# --- narrowing a track's span to the audio it actually has ------------------

class _Seg:
    def __init__(self, name, start_wall):
        self.path = type("P", (), {"name": name})()
        self.start_wall = start_wall


def _segment(start_wall, end_wall):
    from karaoke.recording_slice import Segment

    return Segment(artist="A", title="T", start_wall=start_wall,
                   end_wall=end_wall, marks=2, spread=0.5)


def test_a_full_track_keeps_its_whole_span():
    from karaoke.silence import Silence

    seg = _segment(1000.0, 1240.0)
    files = [_Seg("seg-a.flac", 1000.0)]
    stored = {"seg-a.flac": [Silence(10.0, 12.0)]}   # a gap in the middle
    assert eval_align.audible_end(seg, files, stored) == pytest.approx(240.0)


def test_a_track_cut_off_by_the_player_is_shortened():
    """Recording 12's last track: 30 seconds in, playback moved elsewhere."""
    from karaoke.silence import Silence

    seg = _segment(1000.0, 1240.0)
    files = [_Seg("seg-a.flac", 1000.0)]
    # Silence from offset 30 to the end of the track's span and beyond.
    stored = {"seg-a.flac": [Silence(30.0, 600.0)]}
    assert eval_align.audible_end(seg, files, stored) == pytest.approx(30.0)


def test_a_span_with_no_silence_map_is_left_alone():
    seg = _segment(1000.0, 1240.0)
    assert eval_align.audible_end(seg, [], {}) == pytest.approx(240.0)
