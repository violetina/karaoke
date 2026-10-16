"""Mood timeline: the pure part (plane, smoothing, swings, borderliners)."""
from unittest.mock import MagicMock

import pytest

from karaoke import mood_timeline as mt


def test_quadrants():
    assert mt.classify(+0.8, +1.0) == ("happy", None)
    assert mt.classify(-0.8, +1.0) == ("angry", None)
    assert mt.classify(-0.8, -1.0) == ("sad", None)
    assert mt.classify(+0.8, -1.0) == ("tender", None)


def test_a_point_near_an_axis_is_borderline_with_the_mood_across_it():
    assert mt.classify(+0.05, -1.0) == ("tender", "sad")     # near valence 0
    assert mt.classify(+0.8, -0.05) == ("tender", "happy")   # near arousal 0


def test_a_steady_song_has_no_swings():
    tl = mt.build([-0.6] * 12, [-1.0] * 12)
    assert [s.mood for s in tl.segments] == ["sad"]
    assert tl.summary["swing_count"] == 0


def test_a_quiet_verse_into_a_loud_section_is_one_swing():
    tl = mt.build([-0.6] * 6 + [-0.6] * 6, [-1.0] * 6 + [1.2] * 6)
    assert [s.mood for s in tl.segments] == ["sad", "angry"]
    swing = tl.summary["swings"][0]
    assert (swing["from"], swing["to"]) == ("sad", "angry")
    assert 40.0 <= swing["at_s"] <= 70.0


def test_a_short_blip_is_absorbed_not_a_swing():
    tl = mt.build([-0.6] * 12, [-1.0] * 5 + [1.5, 1.5] + [-1.0] * 5)
    assert tl.summary["swing_count"] == 0


def test_wobbling_on_a_boundary_does_not_flip_the_mood():
    # Arousal hovering just around zero: hysteresis keeps the running mood.
    arousal = [-0.6, -0.6, -0.6] + [0.05, -0.05] * 4 + [-0.6]
    tl = mt.build([0.6] * len(arousal), arousal)
    assert tl.summary["swing_count"] == 0


def test_summary_shares_and_borderline():
    tl = mt.build([0.05] * 6, [-1.0] * 6, duration_s=60.0)
    assert tl.summary["dominant"] == "tender"
    assert tl.summary["borderline_share"] == 1.0
    assert tl.segments[0].label == "tender↔sad"


def test_at_finds_the_stored_segment():
    stored = {"segments": [{"start_s": 0.0, "end_s": 30.0, "mood": "sad", "alt": None},
                           {"start_s": 30.0, "end_s": 60.0, "mood": "angry", "alt": None}]}
    assert mt.at(stored, 31.0)["mood"] == "angry"
    assert mt.at(stored, 99.0) is None


def test_stage_uses_the_timeline_and_queues_a_missing_one_once(monkeypatch):
    from karaoke import stage_view

    stage_view._mood_cache.clear()
    stage_view._mood_requested.clear()
    stored = {"duration_s": 120.0, "segments": [
        {"start_s": 0.0, "end_s": 120.0, "mood": "sad", "alt": "tender"}]}
    monkeypatch.setattr("karaoke.localcache.connect", lambda *a, **k: MagicMock())
    monkeypatch.setattr("karaoke.mood_timeline.load", lambda tid, conn: stored if tid == 1 else None)
    queued = []
    monkeypatch.setattr("karaoke.postprocess_queue.enqueue_if_needed",
                        lambda a, t, *r, **k: queued.append((a, t)))
    seg = stage_view._mood_segment(1, "A", "T", 10.0, 121.0)
    assert (seg["mood"], seg["alt"]) == ("sad", "tender")
    assert stage_view._mood_segment(2, "B", "U", 10.0, 0.0) is None
    stage_view._mood_cache.clear()
    assert stage_view._mood_segment(2, "B", "U", 10.0, 0.0) is None
    assert queued == [("B", "U")]


def test_a_different_edit_shows_no_timeline_mood(monkeypatch):
    from karaoke import stage_view

    stage_view._mood_cache.clear()
    stored = {"duration_s": 200.0, "segments": [
        {"start_s": 0.0, "end_s": 200.0, "mood": "sad", "alt": None}]}
    monkeypatch.setattr("karaoke.localcache.connect", lambda *a, **k: MagicMock())
    monkeypatch.setattr("karaoke.mood_timeline.load", lambda tid, conn: stored)
    assert stage_view._mood_segment(1, "A", "T", 10.0, 260.0) is None


def test_embed_clap_also_computes_the_mood_timeline(monkeypatch, tmp_path):
    from karaoke import postprocess_worker, tasks
    from karaoke.tasks import PostprocessContext

    calls = []
    monkeypatch.setattr(postprocess_worker, "run_clap_logic", lambda *a, **k: calls.append("clap"))
    monkeypatch.setattr(postprocess_worker, "run_mood_logic", lambda *a, **k: calls.append("mood"))
    monkeypatch.setattr("karaoke.localcache.connect", MagicMock())
    (tmp_path / "a.webm").write_bytes(b"x")
    ctx = PostprocessContext(artist="A", title="B", url="u", track_id=3,
                             audio_path=tmp_path / "a.webm", pending=["mood"])
    tasks.embed_clap.run(ctx.to_dict())
    assert calls == ["mood"]
