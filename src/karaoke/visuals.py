"""Aggregate sentiment + rhythm visuals for the karaoke free-space panel.

Pure and dependency-free: turns a block of lyric text into a coarse mood profile
and a small ASCII "sentiment arc", and turns a BPM/energy into a simple rhythm
bar. This is a creative vibe cue, not real affect analysis — it builds on the
lexicon in ``sentiment``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .sentiment import MOODS, mood_of, score_line

# Glyph + accent per mood for the arc/labels.
_MOOD_MARK = {
    "happy": "▲",
    "sad": "▽",
    "angry": "✷",
    "tender": "♥",
    "neutral": "·",
}


@dataclass(frozen=True)
class SentimentProfile:
    """Coarse mood breakdown of a lyric block."""

    counts: dict[str, int]
    dominant: str
    total_hits: int
    line_moods: list[str] = field(default_factory=list)

    @property
    def shares(self) -> dict[str, float]:
        """Fraction of mood hits per mood (0..1)."""
        if self.total_hits <= 0:
            return {m: 0.0 for m in MOODS if m != "neutral"}
        return {m: c / self.total_hits for m, c in self.counts.items()}


def analyze_sentiment(text: str) -> SentimentProfile:
    """Aggregate per-line moods across a lyric block into a profile."""
    counts = {"happy": 0, "sad": 0, "angry": 0, "tender": 0}
    line_moods: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        s = score_line(line)
        for mood in counts:
            counts[mood] += s.get(mood, 0)
        line_moods.append(mood_of(line))
    total = sum(counts.values())
    dominant = max(counts, key=lambda m: counts[m]) if total else "neutral"
    return SentimentProfile(counts, dominant if total else "neutral",
                            total, line_moods)


def sentiment_arc(profile: SentimentProfile, width: int = 24) -> str:
    """A one-line ASCII arc of the per-line moods (down-sampled to `width`)."""
    moods = profile.line_moods
    if not moods:
        return "·" * width
    out = []
    for i in range(width):
        idx = int(i * len(moods) / width)
        out.append(_MOOD_MARK.get(moods[idx], "·"))
    return "".join(out)


def sentiment_bars(profile: SentimentProfile, width: int = 12) -> str:
    """Small horizontal bar chart of mood shares."""
    shares = profile.shares
    lines = []
    for mood in ("happy", "tender", "sad", "angry"):
        filled = int(round(shares.get(mood, 0.0) * width))
        bar = "█" * filled + "░" * (width - filled)
        lines.append(f"{_MOOD_MARK[mood]} {mood:6s} {bar}")
    return "\n".join(lines)


def rhythm_bar(bpm: float | None, elapsed: float = 0.0, width: int = 16) -> str:
    """A tiny animated rhythm indicator driven by BPM and elapsed time.

    Returns a bar with a moving pulse whose position advances one beat at a
    time. With no BPM, returns a static dashed bar.
    """
    if not bpm or bpm <= 0:
        return "‑" * width + "  (bpm ?)"
    beat = 60.0 / bpm
    pos = int((elapsed / beat) % width) if beat > 0 else 0
    cells = ["·"] * width
    cells[pos] = "●"
    return "".join(cells) + f"  {bpm:.0f} bpm"


def tempo_word(bpm: float | None) -> str:
    """Rough Italian tempo marking for a BPM (a fun, human label)."""
    if not bpm or bpm <= 0:
        return "unknown"
    if bpm < 60:
        return "largo (very slow)"
    if bpm < 76:
        return "adagio (slow)"
    if bpm < 108:
        return "andante (walking)"
    if bpm < 120:
        return "moderato"
    if bpm < 156:
        return "allegro (fast)"
    if bpm < 176:
        return "vivace (lively)"
    return "presto (very fast)"


_CARTWHEEL_FRAMES = [
    [
        "   o   ",
        "  /|\\  ",
        "  / \\  "
    ],
    [
        " \\ o / ",
        "   |   ",
        "  / \\  "
    ],
    [
        "  _ o  ",
        "   /\\  ",
        "  | \\  "
    ],
    [
        "   __\\ ",
        " ___\\o ",
        " /)  | "
    ],
    [
        "  __|  ",
        "  \\o   ",
        "  ( \\  "
    ],
    [
        "  \\ /  ",
        "   |   ",
        "  /o\\  "
    ],
    [
        "  |__  ",
        "   o/  ",
        "  / )  "
    ],
    [
        "  o _  ",
        "  /\\   ",
        "  / |  "
    ],
    [
        "   o   ",
        "  /|\\  ",
        "  / \\  "
    ]
]


def cartwheel_frame(bpm: float | None, elapsed: float, max_width: int = 24) -> str:
    """Return an animated ASCII art cartwheel frame driven by the beat.

    The figure rolls from left to right over a 4-beat cycle.
    """
    if not bpm or bpm <= 0:
        bpm = 120.0  # default to a nice 120 BPM tempo
    
    beat_duration = 60.0 / bpm
    if beat_duration <= 0:
        beat_duration = 0.5
    
    # Progress through a single beat (drives the 9-frame cartwheel rotation)
    progress = (elapsed % beat_duration) / beat_duration
    frame_idx = int(progress * len(_CARTWHEEL_FRAMES)) % len(_CARTWHEEL_FRAMES)
    
    # Progress through a 4-beat cycle (drives the left-to-right travel)
    cycle_duration = 4.0 * beat_duration
    cycle_progress = (elapsed % cycle_duration) / cycle_duration
    
    # Calculate horizontal displacement (padding)
    # The figure itself is 7 chars wide. With max_width=24, max padding is 24 - 7 = 17.
    figure_width = 7
    max_padding = max(0, max_width - figure_width)
    padding_size = int(cycle_progress * max_padding)
    padding = " " * padding_size
    
    # Prepend padding to each line of the selected frame
    lines = [padding + line for line in _CARTWHEEL_FRAMES[frame_idx]]
    return "\n".join(lines)
