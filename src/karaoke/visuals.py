"""Aggregate sentiment + rhythm visuals for the karaoke free-space panel.

Pure and dependency-free: turns a block of lyric text into a coarse mood profile
and a small ASCII "sentiment arc", and turns a BPM/energy into a simple rhythm
bar. This is a creative vibe cue, not real affect analysis — it builds on the
lexicon in ``sentiment``.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from .sentiment import MOODS, mood_of, score_line

# Glyph per mood, used by the arc only. These are East-Asian *ambiguous* width,
# so never place them where something after them has to stay aligned — see
# sentiment_bars.
_MOOD_MARK = {
    "happy": "▲",
    "sad": "▽",
    "angry": "✷",
    "tender": "♥",
    "neutral": "·",
}

# Width of the bar-chart label column: the longest label is "tender". ASCII
# only, so len() and display width agree.
_BAR_LABEL_W = 6


def cell_width(text: str, *, ambiguous: int = 1) -> int:
    """Terminal cell width of ``text``.

    Agrees with ``rich.cells.cell_len`` under the default ``ambiguous=1``, but
    keeps the policy an explicit argument rather than an inherited guess: East-
    Asian "Ambiguous" characters are genuinely terminal-dependent, and callers
    that know their terminal draws them wide can say so.
    """
    total = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue                      # combining marks add no width
        eaw = unicodedata.east_asian_width(ch)
        if eaw in ("W", "F"):
            total += 2
        elif eaw == "A":
            total += ambiguous
        else:
            total += 1
    return total


def pad_cells(text: str, width: int, *, align: str = "center") -> str:
    """Pad ``text`` with spaces to ``width`` terminal cells.

    Never truncates: a string already wider than ``width`` is returned as-is,
    since silently cutting a glyph is worse than overflowing by one cell.
    """
    pad = max(0, width - cell_width(text))
    if align == "center":
        left = pad // 2
        return " " * left + text + " " * (pad - left)
    if align == "right":
        return " " * pad + text
    return text + " " * pad


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
    """Small horizontal bar chart of mood shares.

    Deliberately label-then-bar with NO leading glyph. The marks in
    ``_MOOD_MARK`` are East-Asian *ambiguous* width: terminals disagree about
    whether they occupy one cell or two, and font fallback can differ per glyph
    — one report had "▽" drawn wide while "▲ ♥ ✷" stayed narrow, so only the
    "sad" bar was pushed a column right. Nothing can measure that reliably
    (``rich.cells.cell_len`` reports 1 for all four), so the fix is to put
    nothing mis-measurable before the bar. Everything left of it is ASCII, and
    every bar therefore starts in the same column on every terminal.

    The marks still appear in :func:`sentiment_arc`, where a shift is harmless.
    """
    shares = profile.shares
    lines = []
    for mood in ("happy", "tender", "sad", "angry"):
        filled = int(round(shares.get(mood, 0.0) * width))
        # NB "█"/"░" are themselves ambiguous-width. Terminals seen so far draw
        # them narrow; if one ever doesn't, swap them for "#"/"." here.
        bar = "█" * filled + "░" * (width - filled)
        lines.append(f"{mood:<{_BAR_LABEL_W}s} {bar}")
    return "\n".join(lines)


# How much of each beat the pulse spends in the air. Short: the hop should read
# as a strike on the beat, not as hovering between them.
HOP_FRACTION = 0.35

# Beats per traverse of the bar. Two gives a visible sweep at ordinary tempos
# without the pulse becoming a blur at fast ones.
BOUNCE_BEATS = 2.0


def rhythm_bar(bpm: float | None, elapsed: float = 0.0, width: int = 16) -> str:
    """An animated rhythm indicator driven by BPM and elapsed time.

    Two rows. The pulse travels left and right and **reverses at each end**
    rather than wrapping: a sawtooth that teleports back to the start reads as
    drift, and the eye follows the jump rather than the beat.

    On each beat the pulse is drawn on the upper row and lands on the lower one
    between beats. The horizontal travel alone is what a metronome does not
    have; the hop is what actually marks time.

    Depends only on ``elapsed``, so the same instant always renders identically
    and the caller's timer interval cannot introduce jitter.

    With no BPM there is no beat to keep, so it stays a single static bar.
    """
    if not bpm or bpm <= 0:
        return "‑" * width + "  (bpm ?)"
    if width < 1:
        return f"  {bpm:.0f} bpm"

    beat = 60.0 / bpm
    beats = elapsed / beat

    # Triangle wave over BOUNCE_BEATS: 0 -> 1 -> 0, so the pulse turns around at
    # the ends instead of jumping back to the start.
    phase = (beats / BOUNCE_BEATS) % 2.0
    travel = phase if phase <= 1.0 else 2.0 - phase
    pos = min(width - 1, int(travel * (width - 1) + 0.5))

    airborne = (beats % 1.0) < HOP_FRACTION
    upper = ["·"] * width
    lower = ["·"] * width
    (upper if airborne else lower)[pos] = "●"
    return ("".join(upper) + "\n"
            + "".join(lower) + f"  {bpm:.0f} bpm")


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
