"""Lay real lyrics onto Whisper's rhythm.

Whisper on sung audio is a poor transcriber and a good metronome. It mishears
words ("up to do" -> "up to doom", "daddy done" -> "daddy John") and emits
"🎵" / "// Music //" artifacts, but the *times* it attaches to what it heard are
close to right.

So Whisper supplies timing and a known-good source (LRCLIB, Genius) supplies
words. The two cannot simply be zipped: Whisper's line breaks fall in different
places from the real lyric's, and it drops or invents whole phrases.

    whisper: [00:21.24] mister, when you're gone 🎵 🎵They bring you
    real:    Where, mister, when you're young / They bring you up to do

Instead both are reduced to normalized word streams and aligned with
``difflib.SequenceMatcher``, which anchors on the words Whisper *did* get right
("valley", "high school", "seventeen") and tolerates the rest as edits. Each
real line takes the timestamp of its earliest anchored word; unanchored lines
are interpolated between their neighbours so no line is left without a time.

stdlib only — no alignment dependency is pulled in for this.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable, Optional

# Whisper marks instrumental passages with these; they anchor nothing.
_ARTIFACT = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]|//\s*music\s*//",
                       re.IGNORECASE)

_WORD = re.compile(r"[a-z0-9']+")

# Numbers appear as digits in one source and words in the other often enough
# ("17" vs "seventeen") to be worth anchoring on.
_NUMBERS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
    "6": "six", "7": "seven", "8": "eight", "9": "nine", "10": "ten",
    "11": "eleven", "12": "twelve", "13": "thirteen", "14": "fourteen",
    "15": "fifteen", "16": "sixteen", "17": "seventeen", "18": "eighteen",
    "19": "nineteen", "20": "twenty",
}


def normalize_word(word: str) -> str:
    """Reduce a word to a comparable token, or "" if it carries no signal."""
    w = _ARTIFACT.sub(" ", word or "").casefold()
    # Contractions differ between sources ("we'd" vs "wed"); drop apostrophes.
    w = w.replace("’", "'").replace("'", "")
    m = _WORD.search(w)
    if not m:
        return ""
    token = m.group(0)
    return _NUMBERS.get(token, token)


def _lyric_tokens(lines: list[str]) -> tuple[list[str], list[int]]:
    """Flatten lyric lines to (tokens, line index per token)."""
    tokens: list[str] = []
    owners: list[int] = []
    for i, line in enumerate(lines):
        for raw in line.split():
            tok = normalize_word(raw)
            if tok:
                tokens.append(tok)
                owners.append(i)
    return tokens, owners


def _whisper_tokens(words: Iterable) -> tuple[list[str], list[float]]:
    """Flatten timestamped Whisper words to (tokens, start time per token)."""
    tokens: list[str] = []
    starts: list[float] = []
    for w in words:
        for raw in str(getattr(w, "text", "")).split():
            tok = normalize_word(raw)
            if tok:
                tokens.append(tok)
                starts.append(float(getattr(w, "start", 0.0)))
    return tokens, starts


def _interpolate(times: list[Optional[float]], total: Optional[float]) -> list[float]:
    """Fill gaps in a partially-timed line list, keeping it non-decreasing.

    A line Whisper never anchored still has to appear at a sensible moment, so
    it is spread evenly between the nearest timed lines on either side.
    """
    n = len(times)
    out: list[float] = [0.0] * n
    known = [i for i, t in enumerate(times) if t is not None]
    if not known:
        # Nothing anchored: fall back to an even spread over the track.
        span = total or float(n)
        return [i * span / max(n, 1) for i in range(n)]

    for i, t in enumerate(times):
        if t is not None:
            out[i] = t
            continue
        prev = max((k for k in known if k < i), default=None)
        nxt = min((k for k in known if k > i), default=None)
        if prev is None:                      # leading untimed lines
            out[i] = max(0.0, times[nxt] - (nxt - i))          # type: ignore[operator]
        elif nxt is None:                     # trailing untimed lines
            tail = total if total and total > times[prev] else times[prev] + (n - prev)  # type: ignore[operator]
            step = (tail - times[prev]) / (n - prev)           # type: ignore[operator]
            out[i] = times[prev] + step * (i - prev)           # type: ignore[operator]
        else:
            step = (times[nxt] - times[prev]) / (nxt - prev)   # type: ignore[operator]
            out[i] = times[prev] + step * (i - prev)           # type: ignore[operator]

    # Enforce monotonicity; a lyric line may never move backwards in time.
    for i in range(1, n):
        if out[i] < out[i - 1]:
            out[i] = out[i - 1]
    return out


def align_lines(
    lyric_lines: list[str],
    words: Iterable,
    *,
    total_duration: Optional[float] = None,
) -> list[tuple[float, str]]:
    """Assign a timestamp to each real lyric line from Whisper word timings.

    Returns ``(seconds, line_text)`` with the ORIGINAL line text — punctuation,
    capitalisation and all. Only the timings come from Whisper.
    """
    lines = [ln for ln in (lyric_lines or [])]
    if not lines:
        return []

    lyric_toks, owners = _lyric_tokens(lines)
    whisper_toks, starts = _whisper_tokens(words)
    if not lyric_toks or not whisper_toks:
        return list(zip(_interpolate([None] * len(lines), total_duration), lines))

    # Earliest anchored time per line.
    per_line: list[Optional[float]] = [None] * len(lines)
    matcher = SequenceMatcher(None, lyric_toks, whisper_toks, autojunk=False)
    for li, wi, size in matcher.get_matching_blocks():
        for k in range(size):
            line_no = owners[li + k]
            t = starts[wi + k]
            if per_line[line_no] is None or t < per_line[line_no]:  # type: ignore[operator]
                per_line[line_no] = t

    return list(zip(_interpolate(per_line, total_duration), lines))


def align_lyrics_to_lrc(
    plain_lyrics: str,
    words: Iterable,
    *,
    total_duration: Optional[float] = None,
) -> str:
    """Align plain lyrics onto Whisper word timings and render LRC.

    Blank lines are dropped: they are stanza separators in the source text and
    have nothing to sing, so they should not consume a timestamp.
    """
    from .whisper_sync import lines_to_lrc

    lines = [ln.strip() for ln in (plain_lyrics or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    return lines_to_lrc(align_lines(lines, words, total_duration=total_duration))
