"""Scoring rules of scripts/bench_dj_models.py (pure functions, no services)."""
from scripts import bench_dj_models as bench

TRUTH = bench.Truth(track_id=74, artist="The Slits", title="Typical Girls", key="F minor", bpm=162.0,
                    feeling="happy", mood="happy", chords=["Fm", "C#", "G#", "A#m"],
                    members=["Ari Up", "Palmolive", "Viv Albertine"])


def test_key_variants():
    for text in ("It's in F minor", "key of Fm at 162", "F-minor", "Camelot 4A", "E# minor"):
        assert bench.key_mentioned(text, "F minor"), text
    for text in ("F major", "a minor detail", "in C minor", "14A", "F", "of minor importance"):
        assert not bench.key_mentioned(text, "F minor"), text


def test_bpm_tolerance():
    assert bench.bpm_mentioned("blasting at 162 BPM", 162.0)
    assert bench.bpm_mentioned("around 160 bpm", 162.0)
    assert not bench.bpm_mentioned("around 120 BPM", 162.0)
    assert not bench.bpm_mentioned("released in 1979", 162.0)
    for text in ("a BPM of 162", "**BPM of 162**", "BPM: 161", "tempo is around 163", "**162 BPM**"):
        assert bench.bpm_mentioned(text, 162.0), text
    assert not bench.bpm_mentioned("BPM of 120", 162.0)


def test_chords_whole_names_only():
    assert bench.chords_mentioned("Fm, then C# and G#", ["Fm", "C#", "G#"]) == ["Fm", "C#", "G#"]
    assert bench.chords_mentioned("Fmaj7 and Cm", ["Fm", "C"]) == []


def test_score_key_bpm_partial():
    s, _ = bench.score("key_bpm", "F minor, fast!", TRUTH)
    assert s == 0.5
    s, _ = bench.score("key_bpm", "F minor at 162 BPM", TRUTH)
    assert s == 1.0


def test_score_ignores_tags_and_counts_members():
    s, note = bench.score("artist", "Ari Up and Palmolive formed it [FOCUS: The Slits]", TRUTH)
    assert s == 1.0 and "2/3" in note
    s, _ = bench.score("mood", "[QUEUE: X - happy] it is defiant", TRUTH)
    assert s == 0.0


def test_score_suggest(monkeypatch):
    ids = {("Bowie", "Heroes"): 9, ("The Slits", "Typical Girls"): 74}
    monkeypatch.setattr(bench, "resolve_queue",
                        lambda q, conn: ids.get(tuple(p.strip() for p in q.split(" - "))))
    assert bench.score("suggest", "Try [QUEUE: Bowie - Heroes]", TRUTH)[0] == 1.0
    assert bench.score("suggest", "[QUEUE: The Slits - Typical Girls]", TRUTH)[0] == 0.0
    assert bench.score("suggest", "[QUEUE: Nobody - Fake Song]", TRUTH)[0] == 0.25
    assert bench.score("suggest", "no tag here", TRUTH)[0] == 0.0


def test_chord_ranking_by_time_held():
    chords = [{"name": "C", "at": 0, "end": 1}, {"name": "Fm", "at": 1, "end": 5},
              {"name": "N", "at": 5, "end": 9}, {"name": "C", "at": 9, "end": 10}]
    assert bench.chord_ranking(chords) == ["Fm", "C"]


def test_report_renders(tmp_path):
    r = bench.Result(model="m", qid="mood", question="q", answer="happy", ttft_s=1.0, total_s=2.0,
                     score=1.0, note="ok", load={"gpu_avg": 50.0, "cpu_avg": 10.0,
                                                 "gpu_mem_mb_max": 2048.0, "ram_mb_max": 8192.0})
    md = bench.report(["m"], [TRUTH], {"m": {"load_s": 1.2, "where": "100% GPU"}}, [r])
    assert "| m | 1.2s (100% GPU) | 1.0/1 | 1.0s | 2.0s | 50% | 10% | 2.0 GiB | 8.0 GiB | 0 |" in md
