"""Choosing and making images that match a feeling.

The mood square used to spend eight rows saying one word. This picks a picture
instead: score the cached cover images by how well their colour matches the
current mood, or — when nothing cached fits — compute an image from what the
track's own analysis says about it.

Two things about colour that are easy to get wrong and are handled here:

- **Hue is circular.** Red sits at both 0.0 and 1.0, so subtracting hues makes
  neighbours look like opposites. Both the distance and the *average* wrap: the
  mean is taken as a vector on the colour wheel, because arithmetically
  averaging a red at 0.02 and a red at 0.98 gives cyan.
- **Grey has no hue.** An unsaturated pixel's hue is arbitrary, so hues are
  weighted by saturation when averaged. Without that, a black-and-white cover
  votes as loudly as a vivid one for whatever hue rounding happened to produce.

Everything is a pure function over ``list[list[(r, g, b)]]`` — the exact shape
:func:`karaoke.coverart.sample` returns — so it is testable with no images and
no ffmpeg.
"""
from __future__ import annotations

import colorsys
import math
from dataclasses import dataclass
from typing import Optional

Pixels = list[list[tuple[int, int, int]]]


@dataclass(frozen=True)
class MoodTarget:
    """The colour character a mood is looking for."""

    hue: float          # 0..1 on the colour wheel
    saturation: float   # 0..1
    value: float        # 0..1 brightness
    contrast: float     # 0..1 desired spread of brightness


# Deliberately broad: these select between real album covers, which are not
# going to be exactly any target. What matters is the ordering they induce.
MOOD_TARGETS: dict[str, MoodTarget] = {
    # Warm, bright, saturated — sunlit.
    "happy": MoodTarget(hue=0.12, saturation=0.75, value=0.80, contrast=0.35),
    # Cool, dim, washed out.
    "sad": MoodTarget(hue=0.58, saturation=0.30, value=0.35, contrast=0.20),
    # Red, vivid, and high contrast: the contrast term is what separates a
    # blazing cover from a flat maroon one of the same average colour.
    "angry": MoodTarget(hue=0.99, saturation=0.85, value=0.55, contrast=0.60),
    # Soft pink, gentle, low contrast.
    "tender": MoodTarget(hue=0.90, saturation=0.40, value=0.75, contrast=0.15),
    "neutral": MoodTarget(hue=0.55, saturation=0.20, value=0.55, contrast=0.30),
}

# How much each term counts. Hue leads, but not so far that it drags a
# perfectly-hued but lifeless image above a vivid near-miss.
_W_HUE, _W_SAT, _W_VAL, _W_CONTRAST = 0.40, 0.25, 0.20, 0.15

# Semitones to hue: the twelve pitch classes laid around the colour wheel, so
# neighbouring keys get neighbouring colours and the octave closes the circle.
_SEMITONE_HUE = 1.0 / 12.0


@dataclass(frozen=True)
class Stats:
    """Aggregate colour character of an image."""

    hue: float
    saturation: float
    value: float
    contrast: float


def hue_distance(a: float, b: float) -> float:
    """Shortest distance between two hues, 0..0.5.

    Wrapping matters: 0.99 and 0.01 are both red and two hundredths apart, not
    almost a full turn.
    """
    diff = abs((a % 1.0) - (b % 1.0))
    return min(diff, 1.0 - diff)


def colour_stats(pixels: Pixels) -> Stats:
    """Mean hue, saturation, value and brightness spread of an image."""
    flat = [px for row in pixels for px in row]
    if not flat:
        return Stats(hue=0.0, saturation=0.0, value=0.0, contrast=0.0)

    x = y = 0.0
    sat_total = val_total = 0.0
    values: list[float] = []
    for r, g, b in flat:
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        # Saturation-weighted, as a vector: a grey pixel's hue is meaningless
        # and must not vote, and hues have to be averaged around the wheel.
        angle = h * 2.0 * math.pi
        x += math.cos(angle) * s
        y += math.sin(angle) * s
        sat_total += s
        val_total += v
        values.append(v)

    hue = 0.0 if (x == 0.0 and y == 0.0) else (math.atan2(y, x) / (2.0 * math.pi)) % 1.0
    count = len(flat)
    mean_v = val_total / count
    variance = sum((v - mean_v) ** 2 for v in values) / count
    return Stats(hue=hue, saturation=sat_total / count, value=mean_v,
                 contrast=math.sqrt(variance))


def mood_score(pixels: Pixels, mood: str) -> float:
    """How well an image matches a mood, 0..1. Higher is better."""
    target = MOOD_TARGETS.get(mood, MOOD_TARGETS["neutral"])
    stats = colour_stats(pixels)

    # Halved because hue_distance maxes at 0.5; this puts every term on 0..1.
    hue_term = 1.0 - (hue_distance(stats.hue, target.hue) / 0.5)
    sat_term = 1.0 - abs(stats.saturation - target.saturation)
    val_term = 1.0 - abs(stats.value - target.value)
    contrast_term = 1.0 - abs(stats.contrast - target.contrast)

    # A washed-out image has no reliable hue, so let the hue term count for less
    # when there is barely any colour to judge.
    hue_weight = _W_HUE * min(1.0, stats.saturation * 3.0)
    total = hue_weight + _W_SAT + _W_VAL + _W_CONTRAST
    score = (hue_term * hue_weight + sat_term * _W_SAT
             + val_term * _W_VAL + contrast_term * _W_CONTRAST) / total
    return max(0.0, min(1.0, score))


def _key_hue(analysis) -> Optional[float]:
    """Base hue from the track's key, or None when it has none."""
    key = getattr(analysis, "resolved_key", None) or getattr(analysis, "detected_key", None)
    if key is None:
        return None
    # Key objects expose a pitch class; fall back to hashing the name so an
    # unfamiliar representation still yields a stable colour rather than none.
    pitch = getattr(key, "pitch_class", None)
    if pitch is None:
        pitch = getattr(key, "tonic", None)
    if not isinstance(pitch, int):
        name = getattr(key, "name", "") or str(key)
        pitch = sum(ord(c) for c in name) % 12
    return (pitch % 12) * _SEMITONE_HUE


def _is_minor(analysis) -> bool:
    key = getattr(analysis, "resolved_key", None) or getattr(analysis, "detected_key", None)
    name = (getattr(key, "name", "") or str(key or "")).lower()
    return "minor" in name


def generate(analysis, mood: str, cols: int, rows: int) -> Pixels:
    """Compute an image for a track from its own analysis.

    Used when no cached cover matches, which is the normal case for a
    Spotify-only track that has never had artwork downloaded.

    Everything is derived from the SQLite analysis row rather than the
    OpenSearch audio vector: that row is always present for an analysed track,
    needs no cluster, and cannot be out of date relative to the key and BPM
    shown beside it.

    Deterministic for a given track, so the panel does not shimmer between
    refreshes — the variety comes from tracks differing, not from randomness.
    """
    target = MOOD_TARGETS.get(mood, MOOD_TARGETS["neutral"])
    base_hue = _key_hue(analysis)
    if base_hue is None:
        base_hue = target.hue
    minor = _is_minor(analysis)

    energy = getattr(analysis, "energy", None)
    brightness = getattr(analysis, "brightness", None)
    bpm = getattr(analysis, "bpm", None) or 100.0

    saturation = target.saturation if energy is None else max(0.15, min(1.0, energy * 1.6))
    value = target.value if brightness is None else max(0.15, min(1.0, 0.3 + brightness * 1.4))
    if minor:
        # Minor keys read darker and less colourful, which matches how they are
        # heard and keeps major/minor visibly distinct at a glance.
        saturation *= 0.75
        value *= 0.8

    # Tempo sets the spatial frequency: a fast track gets a busier field. The
    # divisor keeps 60-200 BPM inside roughly one to four cycles across the
    # panel, so it stays a pattern rather than becoming noise.
    freq = 0.6 + (bpm / 120.0)

    out: Pixels = []
    for y in range(max(0, rows)):
        line: list[tuple[int, int, int]] = []
        fy = (y / max(1, rows - 1)) if rows > 1 else 0.0
        for x in range(max(0, cols)):
            fx = (x / max(1, cols - 1)) if cols > 1 else 0.0
            # Two interfering waves at right angles: enough structure to read as
            # designed rather than random, cheap enough to be free.
            wave = (math.sin(fx * math.pi * freq) * math.cos(fy * math.pi * freq)
                    + math.sin((fx + fy) * math.pi * freq * 0.5))
            wave = (wave + 2.0) / 4.0                      # -2..2 -> 0..1
            hue = (base_hue + wave * 0.12) % 1.0           # a narrow band, not a rainbow
            val = max(0.0, min(1.0, value * (0.55 + wave * 0.6)))
            sat = max(0.0, min(1.0, saturation * (0.7 + wave * 0.4)))
            r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
            line.append((int(r * 255), int(g * 255), int(b * 255)))
        out.append(line)
    return out
