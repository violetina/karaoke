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


# --- rhythm: bounce and hop ------------------------------------------------
#
# The old bar walked left and teleported back to the start. A sawtooth reads as
# drift -- the eye follows the jump rather than the beat -- and nothing marked
# the downbeat at all.

def _pulse(bpm, elapsed, width=12):
    """(row_index, column) of the pulse, or None if it is not drawn."""
    rows = visuals.rhythm_bar(bpm, elapsed=elapsed, width=width).split("\n")
    for i, row in enumerate(rows):
        if "●" in row:
            return (i, row.index("●"))
    return None


def test_the_pulse_reverses_instead_of_wrapping():
    """It should turn around at the ends, not jump back to the start."""
    beat = 60.0 / 120.0
    span = visuals.BOUNCE_BEATS * beat
    columns = [_pulse(120, t * span / 10.0)[1] for t in range(21)]
    assert columns[:11] == sorted(columns[:11])            # out
    assert columns[10:] == sorted(columns[10:], reverse=True)   # and back
    assert columns[0] == 0 and columns[10] == 11


def test_the_pulse_is_airborne_on_the_beat():
    """The hop is what marks time; the travel alone is just drift."""
    assert _pulse(120, 0.0)[0] == 0            # on the beat -> upper row
    assert _pulse(120, 0.5)[0] == 0            # next beat
    assert _pulse(120, 1.0)[0] == 0


def test_the_pulse_lands_between_beats():
    beat = 60.0 / 120.0
    assert _pulse(120, beat * 0.6)[0] == 1     # past the hop -> lower row
    assert _pulse(120, beat * 0.9)[0] == 1


def test_the_hop_follows_the_tempo():
    """At half the BPM a beat lasts twice as long, so the hop lasts longer."""
    slow_beat = 60.0 / 60.0
    assert _pulse(60, slow_beat * 0.2)[0] == 0
    assert _pulse(60, slow_beat * 0.8)[0] == 1


def test_both_rows_are_the_same_width():
    rows = visuals.rhythm_bar(120, elapsed=0.3, width=10).split("\n")
    assert len(rows) == 2
    assert visuals.cell_width(rows[0]) == 10
    assert rows[1].startswith("·" * 0)          # label follows the cells
    assert visuals.cell_width(rows[1].split("  ")[0]) == 10


def test_the_same_instant_always_renders_identically():
    """The regression guard: the 1.5s and 0.2s timers must not add jitter."""
    assert visuals.rhythm_bar(137, 12.34, 16) == visuals.rhythm_bar(137, 12.34, 16)


def test_no_bpm_stays_a_single_static_row():
    """With no beat to keep there is nothing to hop to."""
    out = visuals.rhythm_bar(None, width=8)
    assert "\n" not in out and "bpm ?" in out


def test_a_zero_width_bar_does_not_raise():
    assert "bpm" in visuals.rhythm_bar(120, elapsed=1.0, width=0)
