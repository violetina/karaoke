"""Choosing which cached cover to show for a feeling.

ffmpeg is faked throughout: what matters here is the selection policy, not the
decoding, which coverart already covers.
"""
import random
from pathlib import Path

import pytest

from karaoke import moodframe


@pytest.fixture()
def pool(tmp_path):
    paths = []
    for i in range(12):
        p = tmp_path / f"cover{i:02d}"
        p.write_bytes(b"x")
        paths.append(p)
    return paths


def _fake_sample(scores, missing=()):
    """Return a grid whose mood_score is stubbed via the path -> score map."""
    def sample(path, cols, rows, **kw):
        if path.name in missing:
            return None
        return [[(0, 0, 0)] * cols for _ in range(rows)]
    return sample


def _fake_score(scores):
    def score(pixels, mood):
        return scores.get(id(pixels), 0.5)
    return score


# -- the pool -----------------------------------------------------------

def test_art_pool_lists_cached_covers(tmp_path):
    (tmp_path / "a").write_bytes(b"x")
    (tmp_path / "b").write_bytes(b"x")
    assert len(moodframe.art_pool(tmp_path)) == 2


def test_art_pool_ignores_partial_downloads(tmp_path):
    """A .part file is a fetch in flight, not an image."""
    (tmp_path / "a").write_bytes(b"x")
    (tmp_path / "b.part").write_bytes(b"x")
    assert [p.name for p in moodframe.art_pool(tmp_path)] == ["a"]


def test_art_pool_of_a_missing_directory_is_empty(tmp_path):
    assert moodframe.art_pool(tmp_path / "nope") == []


# -- scoring a subset ---------------------------------------------------

def test_only_a_bounded_subset_is_scored(pool, monkeypatch):
    """Cost must not grow with the cache: it is one ffmpeg call per candidate."""
    calls = []

    def sample(path, cols, rows, **kw):
        calls.append(path)
        return [[(10, 10, 10)] * cols for _ in range(rows)]

    monkeypatch.setattr(moodframe.coverart, "sample", sample)
    monkeypatch.setattr(moodframe.moodart, "mood_score", lambda px, m: 0.5)
    moodframe.score_pool(pool, "happy", limit=4, rng=random.Random(1))
    assert len(calls) == 4


def test_scoring_skips_an_unreadable_file(pool, monkeypatch):
    """One bad cache entry must not empty the pool."""
    def sample(path, cols, rows, **kw):
        return None if path.name == "cover00" else [[(0, 0, 0)] * cols
                                                    for _ in range(rows)]

    monkeypatch.setattr(moodframe.coverart, "sample", sample)
    monkeypatch.setattr(moodframe.moodart, "mood_score", lambda px, m: 0.7)
    scored = moodframe.score_pool(pool, "happy", limit=12)
    assert len(scored) == 11


def test_scores_come_back_best_first(pool, monkeypatch):
    order = {p.name: i / 20 for i, p in enumerate(pool)}
    monkeypatch.setattr(moodframe.coverart, "sample",
                        lambda path, c, r, **kw: [[(0, 0, 0)]] )
    monkeypatch.setattr(moodframe.moodart, "mood_score", lambda px, m: 0.5)
    scored = moodframe.score_pool(pool, "happy", limit=12)
    assert [s for s, _ in scored] == sorted([s for s, _ in scored], reverse=True)


# -- choosing among the near-best ---------------------------------------

def test_choose_picks_among_the_near_best_not_the_argmax():
    """Always taking the winner shows the same cover every play."""
    scored = [(0.90, Path("a")), (0.88, Path("b")), (0.87, Path("c")),
              (0.40, Path("d"))]
    picks = {moodframe.choose(scored, rng=random.Random(seed))[1].name
             for seed in range(30)}
    assert picks == {"a", "b", "c"}      # d is too far behind to be eligible


def test_choose_excludes_anything_well_behind():
    scored = [(0.90, Path("a")), (0.10, Path("b"))]
    assert moodframe.choose(scored, rng=random.Random(0))[1].name == "a"


def test_choose_of_nothing_is_none():
    assert moodframe.choose([]) is None


# -- the whole decision -------------------------------------------------

def test_a_good_cover_is_used(pool, monkeypatch):
    monkeypatch.setattr(moodframe.coverart, "sample",
                        lambda path, c, r, **kw: [[(1, 2, 3)] * c for _ in range(r)])
    monkeypatch.setattr(moodframe.moodart, "mood_score", lambda px, m: 0.8)
    pixels, source = moodframe.image_for("happy", None, 4, 2, pool=pool,
                                         rng=random.Random(0))
    assert source == "cover"
    assert len(pixels) == 2 and len(pixels[0]) == 4


def test_a_poor_match_falls_back_to_generated_art(pool, monkeypatch):
    """Computed art is more honest than the least-bad photograph."""
    monkeypatch.setattr(moodframe.coverart, "sample",
                        lambda path, c, r, **kw: [[(1, 2, 3)] * c for _ in range(r)])
    monkeypatch.setattr(moodframe.moodart, "mood_score", lambda px, m: 0.1)
    _, source = moodframe.image_for("happy", None, 4, 2, pool=pool,
                                    rng=random.Random(0))
    assert source == "generated"


def test_an_empty_pool_falls_back_to_generated_art():
    """The normal case for a fresh library of Spotify-only tracks."""
    pixels, source = moodframe.image_for("sad", None, 5, 3, pool=[])
    assert source == "generated"
    assert len(pixels) == 3 and len(pixels[0]) == 5


def test_a_winner_that_cannot_be_resampled_falls_back(pool, monkeypatch):
    """Scored small and then failed at full size: still show something."""
    def sample(path, cols, rows, **kw):
        if cols == moodframe.SCORE_COLS:
            return [[(1, 2, 3)] * cols for _ in range(rows)]
        return None

    monkeypatch.setattr(moodframe.coverart, "sample", sample)
    monkeypatch.setattr(moodframe.moodart, "mood_score", lambda px, m: 0.9)
    _, source = moodframe.image_for("happy", None, 4, 2, pool=pool,
                                    rng=random.Random(0))
    assert source == "generated"


def test_a_zero_sized_panel_asks_for_nothing():
    pixels, source = moodframe.image_for("happy", None, 0, 0, pool=[])
    assert pixels == [] and source == "none"
