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
