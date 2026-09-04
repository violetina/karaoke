"""Pick an image for the current feeling, or make one.

:mod:`karaoke.moodart` knows how to judge and how to draw. This decides *what
to judge*: it samples the cached cover-art pool, scores those against the mood,
and returns one — falling back to generated art when nothing cached is close
enough, which is the normal case early on and for a library of Spotify-only
tracks.

The pool is every cover ever cached, not the current track's own. That is the
point: the panel is showing a *feeling*, not the album, and the track's own
artwork already appears in the sidebar.

Two deliberate choices about randomness:

- A random **subset** of the pool is scored rather than all of it. That bounds
  the cost to a fixed number of ffmpeg calls however large the cache grows, and
  it is also where most of the variety comes from.
- The winner is drawn from everything close to the best, not the single best.
  Always returning the argmax would show the same cover every time the same
  song played, which is the opposite of what was asked for.
"""
from __future__ import annotations

import random
from pathlib import Path
from typing import Optional

from . import coverart, moodart
from .logger import log

Pixels = list[list[tuple[int, int, int]]]

# How many cached covers to score per refresh. Each is one ffmpeg call at a
# tiny resolution (~30ms), so this is the cost knob: raise it for better picks,
# lower it if a track change ever feels sluggish.
POOL_SAMPLE = 8

# Resolution used for judging. Colour statistics do not need detail, and a
# small grid keeps the decode cheap; the winner is re-sampled at full size.
SCORE_COLS, SCORE_ROWS = 12, 12

# Anything within this of the best score is considered just as good, and the
# choice between them is random.
NEAR_BEST = 0.06

# Below this, no cached cover really matches the mood and computed art is the
# more honest answer than the least-bad photograph.
MIN_SCORE = 0.45


def art_pool(directory: Optional[Path] = None) -> list[Path]:
    """Every cached cover image available to choose from."""
    target = directory or coverart.art_cache_dir()
    try:
        return sorted(p for p in target.iterdir()
                      if p.is_file() and not p.name.endswith(".part"))
    except OSError:
        return []


def score_pool(pool: list[Path], mood: str, *,
               limit: int = POOL_SAMPLE,
               rng: Optional[random.Random] = None) -> list[tuple[float, Path]]:
    """Score a random subset of the pool against the mood, best first."""
    chooser = rng or random
    candidates = list(pool)
    if len(candidates) > limit:
        candidates = chooser.sample(candidates, limit)

    scored: list[tuple[float, Path]] = []
    for path in candidates:
        pixels = coverart.sample(path, SCORE_COLS, SCORE_ROWS)
        if pixels is None:
            # A truncated or unreadable cache entry: skip it rather than let one
            # bad file empty the pool.
            log.debug("mood art: could not read %s", path)
            continue
        scored.append((moodart.mood_score(pixels, mood), path))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored


def choose(scored: list[tuple[float, Path]], *,
           rng: Optional[random.Random] = None) -> Optional[tuple[float, Path]]:
    """Pick randomly from the images that scored close to the best."""
    if not scored:
        return None
    best = scored[0][0]
    close = [item for item in scored if best - item[0] <= NEAR_BEST]
    return (rng or random).choice(close)


def image_for(mood: str, analysis, cols: int, rows: int, *,
              pool: Optional[list[Path]] = None,
              rng: Optional[random.Random] = None) -> tuple[Pixels, str]:
    """The image to show for a mood, and where it came from.

    The provenance is returned so the panel can say whether it is looking at a
    real cover or a computed one — a generated image that silently claimed to
    be artwork would be misleading about what the library actually holds.
    """
    if cols < 1 or rows < 1:
        return ([], "none")

    candidates = art_pool() if pool is None else pool
    picked = choose(score_pool(candidates, mood, rng=rng), rng=rng)
    if picked is not None and picked[0] >= MIN_SCORE:
        pixels = coverart.sample(picked[1], cols, rows)
        if pixels is not None:
            return (pixels, "cover")
        log.debug("mood art: winner %s failed to re-sample", picked[1])

    return (moodart.generate(analysis, mood, cols, rows), "generated")
