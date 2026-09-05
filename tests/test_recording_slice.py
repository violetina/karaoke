"""Turning identification markers back into a track list.

This is the part of record mode most likely to be subtly wrong, and it needs no
audio to check: a marker says "at wall-clock T we were X seconds into track Y",
so T - X dates the track's start, and several markers of the same track give
independent estimates that must agree.

A key stored against the wrong track is worse than no key, so the tests care as
much about *declining* a bad boundary as about deriving a good one.
"""
import pytest

from karaoke import recording_slice as rs
from karaoke.recording_slice import (MAX_MARK_GAP_S, Mark, describe,
                                     group_marks, is_confident, segment_from,
                                     segments)


def _m(wall, artist="Portishead", title="Glory Box", offset=None, ok=True):
    return Mark(at_wall=wall, artist=artist, title=title,
                at_offset=offset, ok=ok)


# -- a single marker ----------------------------------------------------

def test_a_marker_dates_the_track_start():
    """Offset back from the marker, then back again by the recognition lead.

    songrec's offset describes the audio it sampled; the marker is timestamped
    when the answer arrives. Ignoring the gap put every boundary a recognition
    cycle late, which was measured at ~13.7s against known LRC timings.
    """
    assert _m(1000.0, offset=30.0).start_estimate == pytest.approx(
        970.0 - rs.RECOGNITION_LEAD_S)


def test_the_recognition_lead_is_a_measured_quantity():
    """Guarding the constant: it came from data and should not drift silently."""
    assert 10.0 <= rs.RECOGNITION_LEAD_S <= 18.0


def test_a_marker_without_an_offset_cannot_date_anything():
    assert _m(1000.0).start_estimate is None


# -- grouping -----------------------------------------------------------

def test_consecutive_marks_of_one_track_form_one_run():
    runs = group_marks([_m(1000, offset=30), _m(1045, offset=75)])
    assert len(runs) == 1 and len(runs[0]) == 2


def test_a_track_change_splits_the_run():
    runs = group_marks([
        _m(1000, offset=30),
        _m(1045, artist="Tricky", title="Hell Is Round The Corner", offset=10),
    ])
    assert [r[0].title for r in runs] == ["Glory Box", "Hell Is Round The Corner"]


def test_matching_is_case_insensitive():
    runs = group_marks([_m(1000, offset=30),
                        _m(1045, artist="PORTISHEAD", title="glory box", offset=75)])
    assert len(runs) == 1


def test_a_failed_identification_ends_the_run_without_discarding_it():
    """Silence or speech between two matches means two plays, not one long one.

    The run before the gap is still real and must survive.
    """
    runs = group_marks([
        _m(1000, offset=30), _m(1045, offset=75),
        _m(1090, ok=False, title=""),
        _m(1135, offset=30),
    ])
    assert len(runs) == 2
    assert len(runs[0]) == 2 and len(runs[1]) == 1


def test_the_same_track_after_a_long_gap_is_a_second_play():
    """Repeat, not one impossibly long segment."""
    runs = group_marks([_m(1000, offset=30),
                        _m(1000 + MAX_MARK_GAP_S + 60, offset=30)])
    assert len(runs) == 2


def test_marks_are_sorted_before_grouping():
    runs = group_marks([_m(1045, offset=75), _m(1000, offset=30)])
    assert len(runs) == 1
    assert runs[0][0].at_wall == 1000


def test_no_marks_no_runs():
    assert group_marks([]) == []


# -- boundaries ---------------------------------------------------------

def test_agreeing_marks_give_a_tight_boundary():
    """Both estimates say the track began at 970."""
    seg = segment_from([_m(1000, offset=30), _m(1045, offset=75)])
    assert seg.start_wall == pytest.approx(970.0 - rs.RECOGNITION_LEAD_S)
    assert seg.spread == pytest.approx(0.0)
    assert is_confident(seg)


def test_one_bad_offset_does_not_drag_the_start():
    """Median, not mean -- a single wrong match must not move the boundary."""
    seg = segment_from([_m(1000, offset=30),    # -> 970
                        _m(1045, offset=75),    # -> 970
                        _m(1090, offset=5)])    # -> 1085, wrong
    # The mean would sit ~38s later; the lead shifts both equally.
    assert seg.start_wall == pytest.approx(970.0 - rs.RECOGNITION_LEAD_S)


def test_disagreement_is_reported_and_blocks_analysis():
    # 1000-30 = 970 against 1045-5 = 1040: the two marks disagree by 70s.
    seg = segment_from([_m(1000, offset=30), _m(1045, offset=5)])
    assert seg.spread == pytest.approx(70.0)
    assert not is_confident(seg)


def test_marks_without_offsets_can_never_be_confident():
    """Nothing dates the start, so the boundary is a guess."""
    seg = segment_from([_m(1000), _m(1045)])
    assert seg.spread == float("inf")
    assert not is_confident(seg)


def test_a_single_mark_is_reported_but_not_confident():
    """Nothing corroborates it; a reviewer should see it, analysis should not."""
    seg = segment_from([_m(1000, offset=30)])
    assert seg.marks == 1
    assert seg.spread == pytest.approx(0.0)
    assert not is_confident(seg)


def test_segment_from_nothing_is_an_error():
    with pytest.raises(ValueError):
        segment_from([])


# -- the whole track list -----------------------------------------------

def test_a_track_ends_where_the_next_one_starts():
    segs = segments([
        _m(1000, offset=30), _m(1045, offset=75),          # starts 970
        _m(1100, artist="Tricky", title="Hell", offset=10),
        _m(1145, artist="Tricky", title="Hell", offset=55),  # starts 1090
    ])
    assert len(segs) == 2
    boundary = 1090.0 - rs.RECOGNITION_LEAD_S
    assert segs[0].end_wall == pytest.approx(boundary)
    assert segs[1].start_wall == pytest.approx(boundary)


def test_the_last_track_runs_to_its_last_mark_plus_what_was_heard():
    segs = segments([_m(1000, offset=30), _m(1045, offset=75)])
    # 1045 + 75, less the recognition lead: this end is inferred from the
    # same biased clock as the start, so it carries the same correction.
    assert segs[0].end_wall == pytest.approx(
        1120.0 - rs.RECOGNITION_LEAD_S)


def test_duration_is_derived_from_the_boundaries():
    segs = segments([_m(1000, offset=30), _m(1045, offset=75)])
    assert segs[0].duration == pytest.approx(150.0)    # 970 -> 1120


def test_an_empty_recording_yields_no_tracks():
    assert segments([]) == []


def test_a_recording_of_only_failures_yields_no_tracks():
    assert segments([_m(1000, ok=False, title=""),
                     _m(1045, ok=False, title="")]) == []


def test_describe_flags_confidence():
    good = segments([_m(1000, offset=30), _m(1045, offset=75)])[0]
    poor = segment_from([_m(1000, offset=30)])
    assert describe(good).startswith("ok ")
    assert describe(poor).startswith("?")
    assert "Glory Box" in describe(good)
