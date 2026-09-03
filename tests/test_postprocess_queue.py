"""Tests for post-processing gap detection and enqueue gating."""
from __future__ import annotations

from karaoke import localcache, track_analysis
from karaoke.lyrics import Lyrics
from karaoke.musictheory import parse_key
from karaoke.postprocess_queue import needs_postprocessing


PLAIN_SYNCED = "[00:01.00]hello\n[00:02.00]world"
ENHANCED_SYNCED = "[00:01.00]<00:01.00>hello <00:01.50>world"


def _seed_track(conn, *, synced: str) -> int:
    localcache.add_track_and_lyrics(
        "Kate Earl", "Baddabing Baddaboom",
        Lyrics(synced_raw=synced, plain="hello\nworld", source="lrclib",
               lines=[(1.0, "hello"), (2.0, "world")]),
        url="https://www.youtube.com/watch?v=abc123DEF45", kind="youtube", conn=conn,
    )
    tid = localcache.find_track_id("Kate Earl", "Baddabing Baddaboom", conn)
    assert tid is not None
    return tid


def test_needs_both_when_no_analysis_and_line_level_only(tmp_path):
    conn = localcache.connect(tmp_path / "k.db")
    tid = _seed_track(conn, synced=PLAIN_SYNCED)
    pending = needs_postprocessing(tid, conn)
    assert set(pending) == {"analysis", "timings"}


def test_no_timings_needed_when_word_tags_present(tmp_path):
    conn = localcache.connect(tmp_path / "k.db")
    tid = _seed_track(conn, synced=ENHANCED_SYNCED)
    pending = needs_postprocessing(tid, conn)
    assert "timings" not in pending
    assert "analysis" in pending


def test_no_analysis_needed_once_bpm_stored(tmp_path):
    conn = localcache.connect(tmp_path / "k.db")
    tid = _seed_track(conn, synced=ENHANCED_SYNCED)
    track_analysis.save_detected(
        tid, detected_key=parse_key("A minor"), bpm=107.7,
        method="test", analyzer_version=1, conn=conn,
    )
    pending = needs_postprocessing(tid, conn)
    assert pending == []
