"""Tests for sentiment/rhythm visuals and track-analysis persistence."""

from karaoke import localcache, track_analysis, visuals
from karaoke.lyrics import Lyrics
from karaoke.musictheory import Key


# -- visuals -------------------------------------------------------------
def test_analyze_sentiment_dominant():
    text = "I love you my darling\nhold me close forever\nwe dance and shine"
    profile = visuals.analyze_sentiment(text)
    assert profile.total_hits > 0
    assert profile.dominant in ("tender", "happy")
    assert len(profile.line_moods) == 3


def test_analyze_sentiment_empty():
    profile = visuals.analyze_sentiment("")
    assert profile.dominant == "neutral"
    assert profile.total_hits == 0


def test_sentiment_arc_width():
    profile = visuals.analyze_sentiment("happy joy\nsad tears\nlove heart")
    arc = visuals.sentiment_arc(profile, width=10)
    assert len(arc) == 10


def test_rhythm_bar_moves_with_time():
    at0 = visuals.rhythm_bar(120, elapsed=0.0, width=8)
    # 120 bpm -> 0.5s/beat; at 0.5s the pulse should move one cell
    at_half = visuals.rhythm_bar(120, elapsed=0.5, width=8)
    assert "●" in at0 and "●" in at_half
    assert at0 != at_half
    assert "120 bpm" in at0


def test_rhythm_bar_no_bpm():
    assert "bpm ?" in visuals.rhythm_bar(None)


def test_tempo_word():
    assert "slow" in visuals.tempo_word(50) or "largo" in visuals.tempo_word(50)
    assert "allegro" in visuals.tempo_word(130)
    assert visuals.tempo_word(None) == "unknown"


# -- track analysis DB ---------------------------------------------------
def _seed_track(conn) -> int:
    localcache.add_track_and_lyrics(
        "Ren", "Hi Ren", Lyrics(plain="x", source="manual"), conn=conn
    )
    tid = localcache.find_track_id("Ren", "Hi Ren", conn)
    assert tid is not None
    return tid


def test_save_detected_and_read_back(tmp_path):
    conn = localcache.connect(tmp_path / "k.db")
    tid = _seed_track(conn)
    a = track_analysis.save_detected(
        tid, detected_key=Key(9, "minor"), key_confidence=0.8,
        key_agreement="4/6", bpm=95.0, method="essentia-edma-vote",
        energy=0.72, brightness=0.31, analyzer_version=1, conn=conn,
    )
    assert a.detected_key == Key(9, "minor")
    assert a.bpm == 95.0
    assert a.energy == 0.72
    assert a.brightness == 0.31
    got = track_analysis.get_analysis(tid, conn)
    assert got is not None and got.detected_key == Key(9, "minor")
    assert got.energy == 0.72


def test_verify_key_relative_reconciliation(tmp_path):
    conn = localcache.connect(tmp_path / "k.db")
    tid = _seed_track(conn)
    track_analysis.save_detected(tid, detected_key=Key(9, "minor"), conn=conn)
    # online says C major; detected was A minor -> relatives, resolved = C major
    rec = track_analysis.verify_key(tid, "C major", conn=conn)
    assert rec.agree is True
    assert rec.relation == "relative"
    stored = track_analysis.get_analysis(tid, conn)
    assert stored is not None
    assert stored.reference_key == Key(0, "major")
    assert stored.resolved_key == Key(0, "major")
    assert stored.key_relation == "relative"


# --- bar-chart alignment ---------------------------------------------------
#
# sentiment_bars had no coverage at all, and that is where the alignment bug
# lived: a leading ambiguous-width mood glyph shifted one row's bar sideways.

def _bars(text="happy joy\nsad tears\nlove heart\nhate rage", width=12):
    return visuals.sentiment_bars(visuals.analyze_sentiment(text),
                                  width=width).splitlines()


def test_sentiment_bars_has_a_row_per_mood():
    rows = _bars()
    assert len(rows) == 4
    for mood in ("happy", "tender", "sad", "angry"):
        assert any(r.startswith(mood) for r in rows)


def test_sentiment_bars_all_start_in_the_same_column():
    """The regression test: every bar must begin at the same offset.

    Previously "▽ sad" was drawn one cell wider than its siblings on some
    terminals, so the sad row's bar was pushed right.
    """
    rows = _bars()
    starts = {min(r.index(c) for c in "█░" if c in r) for r in rows}
    assert len(starts) == 1


def test_nothing_mis_measurable_precedes_the_bar():
    """Everything left of the bar is ASCII, so its width cannot be disputed."""
    for row in _bars():
        start = min(row.index(c) for c in "█░" if c in row)
        assert row[:start].isascii()


def test_sentiment_bars_are_exactly_width_cells():
    for row in _bars(width=12):
        bar = row[visuals._BAR_LABEL_W + 1:]
        assert len(bar) == 12
        assert set(bar) <= {"█", "░"}


def test_sentiment_bars_reflect_shares():
    rows = _bars("happy joy sunshine glad", width=12)
    happy = next(r for r in rows if r.startswith("happy"))
    angry = next(r for r in rows if r.startswith("angry"))
    assert happy.endswith("█" * 12)
    assert angry.endswith("░" * 12)


def test_sentiment_bars_empty_profile_is_all_empty():
    for row in _bars("", width=8):
        assert row.endswith("░" * 8)


def test_mood_marks_still_used_by_the_arc():
    """The glyphs are not deleted, only moved out of the aligned rows."""
    profile = visuals.analyze_sentiment("happy joy\nsad tears")
    assert set(visuals.sentiment_arc(profile, width=8)) & set(
        visuals._MOOD_MARK.values())


# --- width helpers ---------------------------------------------------------

def test_cell_width_matches_rich():
    from rich.cells import cell_len
    for glyph in "☀☔🔥♡◇▲▽✷♥·█░ab ":
        assert visuals.cell_width(glyph) == cell_len(glyph), glyph


def test_cell_width_knows_the_genuinely_wide_ones():
    assert visuals.cell_width("☔") == 2
    assert visuals.cell_width("🔥") == 2
    assert visuals.cell_width("☀") == 1


def test_cell_width_ambiguous_policy_is_explicit():
    assert visuals.cell_width("▽", ambiguous=1) == 1
    assert visuals.cell_width("▽", ambiguous=2) == 2


def test_cell_width_ignores_combining_marks():
    assert visuals.cell_width("é") == 1


def test_pad_cells_pads_to_display_width_not_len():
    assert visuals.cell_width(visuals.pad_cells("☔", 2)) == 2
    assert visuals.cell_width(visuals.pad_cells("☀", 2)) == 2
    assert visuals.pad_cells("ab", 6, align="left") == "ab    "
    assert visuals.pad_cells("ab", 6, align="right") == "    ab"


def test_pad_cells_never_truncates():
    assert visuals.pad_cells("abcdef", 2) == "abcdef"
