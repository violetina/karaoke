"""Backstage 'Line Looper' tool: manual loop-and-confirm lyric correction.

The workflow (per the admin-facing spec): loop a lyric line's current
estimated clip, nudge the start in small increments until the loop clearly
begins on the first word, then play through once and mark the moment the
last word is heard. Interior word times are then calculated from those two
confirmed anchors -- NOT an even split. This module reuses the same
line-weighting logic lyric_align.line_weight already uses for line-level
timing (word/character count plus a punctuation pause bonus) at word
granularity, and the same MIN_WORD_S/MAX_WORD_S (or BPM-derived) bounds
lyric_align already enforces, so a manually-corrected line is timed by the
same rules as an automatically-aligned one instead of a separate, cruder
heuristic.
"""
from __future__ import annotations

from typing import Optional

from .lyric_align import MIN_WORD_S, MAX_WORD_S, word_bounds
from .caption_sync import _format_lrc_timestamp, _format_word_timestamp

#: Same punctuation pause weights as lyric_align.line_weight, applied to a
#: single word's trailing/embedded punctuation instead of a whole line's.
_PAUSE_WEIGHTS = {
    ",": 0.4,
    "\u2014": 0.6,  # em dash
    ";": 0.5,
    ".": 0.7,
    "!": 0.8,
    "?": 0.8,
}


def word_weight(word: str) -> float:
    """Roughly how long one word takes to sing, in the same arbitrary units
    as :func:`karaoke.lyric_align.line_weight`.

    Character count (not a flat 1-per-word) keeps long words from being
    rushed relative to short ones; a word ending in comma/period/etc. gets
    the same pause bonus line_weight gives a whole line containing one, so
    a line like "Wait... no!" naturally gets more trailing room than
    "go now".
    """
    text = (word or "").strip()
    if not text:
        return 0.0
    pause_bonus = sum(weight for ch, weight in _PAUSE_WEIGHTS.items() if ch in text)
    return max(0.3, 1.0 + len(text) / 4.0 + pause_bonus)


def distribute_word_times(
    words: list[str],
    start_s: float,
    end_s: float,
    bpm: Optional[float] = None,
) -> list[float]:
    """Place each word's start time between two user-confirmed anchors.

    ``start_s`` is the confirmed loop start (first word's onset);
    ``end_s`` is the moment the user marked hearing the *last* word -- i.e.
    the last word's start, not the line's end. Interior words are placed
    proportional to :func:`word_weight`, not evenly spaced, and clamped to
    :func:`karaoke.lyric_align.word_bounds` so a lopsided weight ratio can
    never push one word to an implausibly short or long span.

    Raises ``ValueError`` if ``end_s`` is not after ``start_s`` (the end
    anchor has to be captured after the start one).
    """
    n = len(words)
    if n == 0:
        return []
    if end_s < start_s:
        raise ValueError("end_s must be >= start_s")
    if n == 1:
        return [start_s]

    span = end_s - start_s
    lo, _hi = word_bounds(bpm) if bpm else (MIN_WORD_S, MAX_WORD_S)
    # The confirmed end anchor is the LAST word's start, so there are only
    # (n - 1) gaps to distribute across the span, weighted by every word
    # except the final one (nothing trails it to make room for).
    weights = [word_weight(w) for w in words[:-1]]
    total_w = sum(weights) or float(n - 1)

    ideal = [start_s]
    cum = 0.0
    for w in weights:
        cum += w
        ideal.append(start_s + span * (cum / total_w))
    ideal[-1] = end_s

    # Forward pass: never let a word start closer than the minimum word gap
    # to the one before it.
    times = [ideal[0]]
    for t in ideal[1:]:
        times.append(max(t, times[-1] + lo))

    if times[-1] > end_s:
        # The span is too tight for even the minimum per-word gap (e.g. many
        # words confirmed inside a fraction of a second) -- fall back to an
        # even split across the whole span rather than hand back a sequence
        # that overshoots the confirmed end or stops being monotonic.
        step = span / (n - 1)
        times = [start_s + i * step for i in range(n)]

    times[-1] = end_s  # the confirmed anchor always wins exactly
    return times


def render_enhanced_lrc_line(start_s: float, words: list[str], word_times: list[float]) -> str:
    """Render one Enhanced-LRC line: ``[mm:ss.xx]<mm:ss.xx>word1 <mm:ss.xx>word2 ...``

    Matches caption_sync.json3_to_enhanced_lrc's own convention exactly (the
    first word tag equals the line timestamp) so a manually-corrected line
    is indistinguishable in format from one derived from real captions.
    """
    stamp = _format_lrc_timestamp(round(start_s * 1000))
    tagged = " ".join(
        f"{_format_word_timestamp(round(t * 1000))}{w}"
        for t, w in zip(word_times, words)
    )
    return f"{stamp} {tagged}"


def replace_line_in_synced_lyrics(synced_raw: str, old_start_s: float, new_line_text: str) -> str:
    """Replace the one raw LRC line starting at ``old_start_s`` with ``new_line_text``.

    Every other line -- including any word tags it already carries -- is
    left byte-for-byte untouched; only the matched line is swapped.

    Raises ``ValueError`` if no line starts at that timestamp (rounding
    drift between the caller's float and the stored text would otherwise
    silently drop the correction).
    """
    old_stamp = _format_lrc_timestamp(round(old_start_s * 1000))
    out_lines = []
    replaced = False
    for raw_line in synced_raw.splitlines():
        if not replaced and raw_line.startswith(old_stamp):
            out_lines.append(new_line_text)
            replaced = True
        else:
            out_lines.append(raw_line)
    if not replaced:
        raise ValueError(f"no line starting at {old_stamp} found to replace")
    return "\n".join(out_lines)
