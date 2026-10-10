"""Tests for post-processing gap detection and enqueue gating."""
from __future__ import annotations

from karaoke import localcache, track_analysis
from karaoke.lyrics import Lyrics
from karaoke.musictheory import parse_key
from karaoke import postprocess_queue as q
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
    pending = needs_postprocessing(tid, conn, include_search_artefacts=False)
    assert set(pending) == {"analysis", "timings"}


def test_no_timings_needed_when_word_tags_present(tmp_path):
    conn = localcache.connect(tmp_path / "k.db")
    tid = _seed_track(conn, synced=ENHANCED_SYNCED)
    pending = needs_postprocessing(tid, conn, include_search_artefacts=False)
    assert "timings" not in pending
    assert "analysis" in pending


def test_no_analysis_needed_once_bpm_stored(tmp_path):
    conn = localcache.connect(tmp_path / "k.db")
    tid = _seed_track(conn, synced=ENHANCED_SYNCED)
    track_analysis.save_detected(
        tid, detected_key=parse_key("A minor"), bpm=107.7,
        method="test", analyzer_version=1, conn=conn,
    )
    pending = needs_postprocessing(tid, conn, include_search_artefacts=False)
    assert pending == []


# -- search artefacts (OpenSearch-derived steps) -------------------------

def test_a_missing_vector_and_chord_doc_become_pending(monkeypatch):
    """Both artefacts live in OpenSearch, not the database."""
    class Empty:
        def exists(self, **kw): return False
        def search(self, **kw): return {"hits": {"hits": []}}
    monkeypatch.setattr("karaoke.osclient.client", lambda *a, **k: Empty())
    assert q._missing_search_artefacts(1) == ["vectors", "chords"]


def test_present_artefacts_are_not_pending(monkeypatch):
    class Full:
        def exists(self, **kw): return True
        def search(self, **kw): return {"hits": {"hits": [{"_id": "x"}]}}
    monkeypatch.setattr("karaoke.osclient.client", lambda *a, **k: Full())
    assert q._missing_search_artefacts(1) == []


def test_an_unreachable_cluster_claims_nothing_is_missing(monkeypatch):
    """The regression this guards: treating a down cluster as "nothing indexed"
    would mark all 18k tracks as needing vectors and flood the queue."""
    class Down:
        def exists(self, **kw): raise RuntimeError("connection refused")
        def search(self, **kw): raise RuntimeError("connection refused")
    monkeypatch.setattr("karaoke.osclient.client", lambda *a, **k: Down())
    assert q._missing_search_artefacts(1) == []

    monkeypatch.setattr("karaoke.osclient.client", lambda *a, **k: None)
    assert q._missing_search_artefacts(1) == []
