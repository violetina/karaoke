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


def test_an_undecodable_file_is_no_audio_not_a_crash(monkeypatch, tmp_path):
    import contextlib

    def broken_wav(path, rate):
        raise RuntimeError("Unspecified internal error.")

    monkeypatch.setattr("karaoke.clap_vector._load", lambda: (None, None))
    monkeypatch.setattr("karaoke.analyze._as_wav", lambda path, rate: contextlib.nullcontext(None))
    assert mt._audio_scores(str(tmp_path / "x.mp3")) is None          # no wav: no audio
    monkeypatch.setattr("karaoke.analyze._as_wav", broken_wav)
    assert mt._audio_scores(str(tmp_path / "x.mp3")) is None          # decoder raises: no audio


def test_the_backfill_survives_a_track_that_raises(monkeypatch, capsys):
    import importlib.util
    import sys
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "backfill_moods", Path(__file__).resolve().parent.parent / "scripts" / "backfill_moods.py")
    script = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = script
    spec.loader.exec_module(script)

    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {"n": "Artist - Song"}
    monkeypatch.setattr("karaoke.localcache.connect", lambda *a, **k: contextlib_null(conn))
    calls = []

    def analyse(tid, c):
        calls.append(tid)
        if tid == 2:
            raise RuntimeError("Unspecified internal error.")
        return None
    monkeypatch.setattr(mt, "analyse_track", analyse)
    assert script.main(["--track-id", "1", "--track-id", "2", "--track-id", "3"]) == 0
    assert calls == [1, 2, 3]                     # kept going after track 2
    assert "1 errors" in capsys.readouterr().out


def contextlib_null(value):
    import contextlib

    return contextlib.nullcontext(value)


def test_a_segment_is_named_by_its_own_average():
    # Hysteresis kept "angry" running into a stretch whose average is warm and
    # calm ("Simple Song" at 3:00); the label must follow the average.
    valence = [-0.5] * 4 + [0.12] * 8
    arousal = [0.9] * 4 + [0.18, -0.12] * 4
    tl = mt.build(valence, arousal)
    for seg in tl.segments:
        assert seg.mood == mt.classify(seg.valence, seg.arousal)[0]
    assert tl.segments[-1].mood != "angry"


# -- feelings ---------------------------------------------------------------

def test_dark_intensity_is_not_anger_without_an_angry_sound_or_words():
    assert mt.feeling_for("angry", None, None) == "intense"
    assert mt.feeling_for("angry", None, {"aggressive": 1.2, "energetic": 0.4}) == "angry"
    assert mt.feeling_for("angry", None, {"energetic": 1.4, "aggressive": 0.1}) == "intense"
    assert mt.feeling_for("angry", None, {"dramatic": 0.9}) == "dramatic"


def test_blues_is_frustrated_not_angry():
    # Tina, on two Hendrix blues: "frustrated++ -- the blues, not angry".
    assert mt.feeling_for("angry", None, {"bluesy": 1.5, "aggressive": 1.0}) == "frustrated"
    assert mt.feeling_for("angry", None, {"aggressive": 1.5}, {"blues": True}) == "frustrated"
    assert mt.feeling_for("sad", None, None, {"blues": True}) == "blue"


def test_angry_words_make_it_angry():
    assert mt.feeling_for("angry", None, {"energetic": 1.0},
                          {"tone": "angry and confrontational"}) == "angry"


def test_sad_edging_into_tender_is_longing():
    assert mt.feeling_for("sad", "tender", {"melancholic": 1.5}) == "longing"
    assert mt.feeling_for("tender", "sad", None) == "longing"


def test_a_weak_character_leaves_the_quadrant_default():
    assert mt.feeling_for("tender", None, {"dreamy": 0.1, "romantic": 0.2}) == "tender"
    assert mt.feeling_for("tender", None, {"dreamy": 1.1, "romantic": 0.2}) == "dreamy"


def test_contrast_bittersweet_and_ironic():
    assert mt.contrast_for(0.7, -0.6) == "bittersweet"           # warm sound, sad words
    assert mt.contrast_for(0.45, -0.87) is None                 # soft-music bias, not a feeling
    assert mt.contrast_for(0.6, 0.5) is None
    assert mt.contrast_for(None, None, {"tone": "cynical and sarcastic"}) == "ironic"


def test_build_names_feelings_per_segment():
    n = 9
    char = [[0.0] * len(mt.CHARACTERS) for _ in range(n)]
    for row in char:
        row[mt.CHARACTERS.index("bluesy")] = 1.4
    tl = mt.build([-0.5] * n, [1.0] * n, character=char,
                  sound_v=[0.7] * n, words_v=[-0.6] * n)
    seg = tl.segments[0]
    assert (seg.mood, seg.feeling, seg.contrast) == ("angry", "frustrated", "bittersweet")
    assert tl.summary["dominant_feeling"] == "frustrated"
    assert tl.summary["contrast"] == "bittersweet"


def test_a_specific_feeling_wins_a_near_tie_with_a_generic_one():
    assert mt.feeling_for("tender", None, {"calm": 0.89, "romantic": 0.86}) == "romantic"
    assert mt.feeling_for("tender", None, {"calm": 1.4, "romantic": 0.5}) == "calm"


def test_prompt_matrices_are_embedded_once_per_process(monkeypatch):
    calls = []
    mt._matrix_cache.clear()
    monkeypatch.setattr(mt, "_build_character_prompt_matrix", lambda: calls.append(1) or "M")
    assert mt._character_prompt_matrix() == "M"
    assert mt._character_prompt_matrix() == "M"
    assert calls == [1]
    mt._matrix_cache.clear()
