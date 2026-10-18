"""Next-song suggestions: fit by sound and mood, strategies as constraints."""
import json
import math

import pytest

from karaoke import dj_suggest as ds
from karaoke.dj_suggest import Track


def T(tid, clap=(1.0, 0.0), cam="8A", bpm=120.0, start=None, end=None, motion=None, style=None):
    return Track(track_id=tid, artist=f"A{tid}", title=f"T{tid}", key="A minor", camelot=cam,
                 bpm=bpm, clap=clap, start=start, end=end, motion=motion, harmony_style=style)


def test_camelot_scores():
    assert ds.camelot_score("8A", "8A") == 1.0
    assert ds.camelot_score("8A", "8B") == 0.8           # relative
    assert ds.camelot_score("8A", "9A") == 0.8
    assert ds.camelot_score("12A", "1A") == 0.8          # wraps around
    assert ds.camelot_score("8A", "10A") == 0.4
    assert ds.camelot_score("8A", "3B") == 0.0
    assert ds.camelot_score(None, "8A") == 0.0


def test_half_and_double_time_count_as_the_same_tempo():
    assert ds.tempo_ratio(120, 120) == 0
    assert ds.tempo_ratio(120, 60) == pytest.approx(0, abs=1e-9)
    assert ds.tempo_ratio(120, 240) == pytest.approx(0, abs=1e-9)
    assert ds.tempo_ratio(120, 132) == pytest.approx(math.log(1.1))


def test_flow_ranks_the_closer_sound_first():
    seed = T(1, clap=(1.0, 0.0))
    near, far = T(2, clap=(0.95, 0.31)), T(3, clap=(0.0, 1.0))
    ranked = ds.rank(seed, [far, near], "flow", 5)
    assert [t.track_id for _, t in ranked] == [2, 3]


def test_mood_handover_breaks_a_sound_tie():
    seed = T(1, end=(0.6, -1.0, "romantic"))
    follows = T(2, start=(0.5, -0.9, "dreamy"))
    jars = T(3, start=(-0.8, 1.5, "intense"))
    assert [t.track_id for _, t in ds.rank(seed, [jars, follows], "flow", 5)] == [2, 3]


def test_harmonic_keeps_only_compatible_keys_and_tempo():
    seed = T(1, cam="8A", bpm=120)
    ok = T(2, cam="9A", bpm=124)
    wrong_key = T(3, cam="3B", bpm=120)
    too_fast = T(4, cam="8A", bpm=140)
    half_time = T(5, cam="8B", bpm=61)
    ids = {t.track_id for _, t in ds.rank(seed, [ok, wrong_key, too_fast, half_time], "harmonic", 5)}
    assert ids == {2, 5}


def test_harmonic_widens_the_tempo_window_when_short():
    seed = T(1, cam="8A", bpm=120)
    ten_percent = T(2, cam="8A", bpm=132)
    assert [t.track_id for _, t in ds.rank(seed, [ten_percent], "harmonic", 1)] == [2]


def test_energy_up_needs_a_more_intense_opening():
    seed = T(1, end=(0.2, -0.5, "calm"))
    lift = T(2, start=(0.3, 0.8, "energetic"))
    drop = T(3, start=(0.3, -1.5, "quiet"))
    assert [t.track_id for _, t in ds.rank(seed, [lift, drop], "energy_up", 5)] == [2]
    assert [t.track_id for _, t in ds.rank(seed, [lift, drop], "cool_down", 5)] == [3]


def test_energy_without_mood_data_uses_tempo():
    seed = T(1, bpm=100)
    assert [t.track_id for _, t in ds.rank(seed, [T(2, bpm=110), T(3, bpm=95)], "energy_up", 5)] == [2]


def test_structural_needs_chord_motion():
    seed = T(1, motion=(1, 0, 0), style="blues")
    same = T(2, motion=(1, 0, 0), style="blues")
    none = T(3)
    assert [t.track_id for _, t in ds.rank(seed, [none, same], "structural", 5)] == [2]


def test_excluded_and_seed_are_never_suggested():
    seed = T(1)
    assert ds.rank(seed, [T(1), T(2)], "flow", 5, excluded={2}) == []


def test_reason_explains_the_choice():
    seed = T(1, cam="8A", bpm=120, end=(0.5, -1, "romantic"))
    cand = T(2, cam="9A", bpm=118, start=(0.4, -1, "dreamy"), clap=(0.99, 0.1))
    r = ds.reason(seed, cand)
    assert "same sound" in r and "romantic → dreamy" in r and "8A→9A" in r and "118 BPM (-2)" in r


def test_suggest_tool_falls_back_when_the_new_engine_finds_nothing(monkeypatch):
    from karaoke import mcp_server

    monkeypatch.setattr(ds, "suggest", lambda *a, **k: [])
    out = json.loads(mcp_server.suggest_next_tracks(track_id=None, strategy="flow", limit=2))
    assert out.get("strategy") in ("harmonic", None) or "error" in out


def test_the_seed_act_is_skipped_and_each_act_appears_once():
    seed = T(1); seed.artist = "The Jimi Hendrix Experience"
    same_act = T(2, clap=(1.0, 0.0)); same_act.artist = "Jimi Hendrix"
    a1 = T(3, clap=(0.9, 0.44)); a1.artist = "Cream"
    a2 = T(4, clap=(0.89, 0.46)); a2.artist = "Cream"
    other = T(5, clap=(0.8, 0.6)); other.artist = "Free"
    ids = [t.track_id for _, t in ds.rank(seed, [same_act, a1, a2, other], "flow", 2)]
    assert ids == [3, 5]


def test_the_seed_act_returns_when_nothing_else_qualifies():
    seed = T(1); seed.artist = "Ella Fitzgerald"
    only = T(2); only.artist = "Ella Fitzgerald feat. Louis Armstrong"
    assert [t.track_id for _, t in ds.rank(seed, [only], "flow", 1)] == [2]


def test_the_seed_act_only_fills_leftover_slots():
    seed = T(1, cam="2A", bpm=100); seed.artist = "The Jimi Hendrix Experience"
    own = T(2, cam="3A", bpm=99, clap=(1.0, 0.0)); own.artist = "Jimi Hendrix"
    other = T(3, cam="2A", bpm=101, clap=(0.6, 0.8)); other.artist = "Sonic Youth"
    ids = [t.track_id for _, t in ds.rank(seed, [own, other], "harmonic", 2)]
    assert ids == [3, 2]          # the closer-sounding own act still comes after
