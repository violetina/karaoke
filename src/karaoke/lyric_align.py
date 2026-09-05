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

# Plausible span for one sung word. Outside this range the timing is not a word
# being sung: below the floor it is a decoding glitch, and above the ceiling
# Whisper has stretched one token across an instrumental passage. Anchoring a
# lyric line on either puts it seconds away from the singing.
MIN_WORD_S = 0.04
MAX_WORD_S = 4.0

# Fastest credible singing, in seconds per unit of line_weight. Measured
# against real anchors on a rock track: comfortable delivery runs 0.4-0.9, so
# anything under this is not a person singing those words -- it is an anchor
# landing on the wrong repeat of a chorus, which is the usual way a
# well-anchored alignment still comes out wrong.
MIN_SECONDS_PER_WEIGHT = 0.12

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


def word_span_ok(start: float, end: float) -> bool:
    """Whether a Whisper word's duration is plausible for sung speech."""
    span = end - start
    return MIN_WORD_S <= span <= MAX_WORD_S


def _whisper_tokens(words: Iterable) -> tuple[list[str], list[float], float]:
    """Flatten timestamped Whisper words to (tokens, starts, last end).

    Words with an implausible span are dropped rather than kept as anchors:
    Whisper attaches one token to a whole instrumental break often enough that
    trusting it drags a lyric line badly off the singing. The end of the last
    usable word is returned too -- it is a real bound on the song's sung
    content, and better than inventing a tail.
    """
    tokens: list[str] = []
    starts: list[float] = []
    last_end = 0.0
    for w in words:
        start = float(getattr(w, "start", 0.0))
        end = float(getattr(w, "end", start))
        if end > start and not word_span_ok(start, end):
            continue
        pieces = [normalize_word(raw) for raw in str(getattr(w, "text", "")).split()]
        pieces = [tok for tok in pieces if tok]
        if not pieces:
            continue
        # A multi-word token spans its own duration, so spread the pieces
        # across it rather than stacking them all on the start.
        step = (end - start) / len(pieces) if end > start else 0.0
        for i, tok in enumerate(pieces):
            tokens.append(tok)
            starts.append(start + step * i)
        last_end = max(last_end, end)
    return tokens, starts, last_end


def line_weight(line: str) -> float:
    """Roughly how long a line takes to sing, in arbitrary units.

    Syllables would be better than words, and words are better than nothing:
    what matters is that "I been wondering where you are" is given more of a
    gap than "what's it for". Counting characters rather than words keeps a
    line of long words from being rushed.
    """
    text = (line or "").strip()
    if not text:
        return 0.0
    # A floor, so a one-word line still occupies a share of the gap.
    return max(1.0, len(_WORD.findall(text.lower())) + len(text) / 12.0)


def _interpolate(times: list[Optional[float]], total: Optional[float],
                 weights: Optional[list[float]] = None) -> list[float]:
    """Fill gaps in a partially-timed line list, keeping it non-decreasing.

    A line Whisper never anchored still has to appear at a sensible moment. It
    is placed in proportion to how much singing precedes it, not at an even
    interval: spacing lines evenly makes a long line and a two-word line take
    the same time, which is what makes an otherwise well-anchored alignment
    feel off the beat.
    """
    n = len(times)
    out: list[float] = [0.0] * n
    known = [i for i, t in enumerate(times) if t is not None]
    if weights is None or len(weights) != n:
        weights = [1.0] * n

    def share(lo: int, hi: int) -> list[float]:
        """Cumulative weight fraction for lines lo+1..hi-1 within (lo, hi)."""
        span = sum(weights[k] for k in range(lo, hi)) or 1.0
        run, out_fracs = 0.0, []
        for k in range(lo, hi):
            run += weights[k]
            out_fracs.append(run / span)
        return out_fracs

    if not known:
        # Nothing anchored at all: fall back to weight-proportional spacing
        # across whatever duration is known.
        span = total or float(n)
        fracs = share(0, n)
        return [0.0] + [span * f for f in fracs[:-1]]

    for i, t in enumerate(times):
        if t is not None:
            out[i] = t
            continue
        prev = max((k for k in known if k < i), default=None)
        nxt = min((k for k in known if k > i), default=None)
        if prev is None:                      # leading untimed lines
            fracs = share(0, nxt)             # type: ignore[arg-type]
            head = times[nxt]                 # type: ignore[index]
            out[i] = max(0.0, head * (fracs[i - 1] if i else 0.0))
        elif nxt is None:                     # trailing untimed lines
            tail = total if total and total > times[prev] else times[prev] + (n - prev)  # type: ignore[operator]
            fracs = share(prev, n)
            out[i] = times[prev] + (tail - times[prev]) * fracs[i - prev - 1]  # type: ignore[operator]
        else:
            fracs = share(prev, nxt)
            out[i] = times[prev] + (times[nxt] - times[prev]) * fracs[i - prev - 1]  # type: ignore[operator]

    # Enforce monotonicity; a lyric line may never move backwards in time.
    for i in range(1, n):
        if out[i] < out[i - 1]:
            out[i] = out[i - 1]
    return out


def _drop_impossible_anchors(per_line: list[Optional[float]],
                             weights: list[float]) -> list[Optional[float]]:
    """Discard anchors that would require singing faster than anyone can.

    Repeated lyrics are where alignment fails: "no one's ready for your war"
    appears eight times, and the matcher is free to anchor a late line to an
    early occurrence. The giveaway is not the anchor itself but the interval it
    leaves -- on this track twenty lines were pinned into four seconds.

    An anchor that leaves too little room for the lines before it is dropped,
    and those lines are interpolated instead. Losing a suspect anchor costs a
    little precision; keeping it compresses whole verses into a blur.
    """
    kept = list(per_line)
    last_index: Optional[int] = None
    for i, t in enumerate(kept):
        if t is None:
            continue
        if last_index is None:
            last_index = i
            continue
        needed = sum(weights[last_index:i]) * MIN_SECONDS_PER_WEIGHT
        if (t - kept[last_index]) < needed:      # type: ignore[operator]
            kept[i] = None                       # implausible; interpolate it
            continue
        last_index = i
    return kept


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
    weights = [line_weight(ln) for ln in lines]
    whisper_toks, starts, last_end = _whisper_tokens(words)
    if not lyric_toks or not whisper_toks:
        return list(zip(_interpolate([None] * len(lines), total_duration, weights),
                        lines))
    # The last sung word bounds the lyrics better than the file's length, which
    # includes outros and silence the words do not cover.
    horizon = total_duration
    if last_end > 0 and (horizon is None or last_end < horizon):
        horizon = last_end

    # Earliest anchored time per line.
    per_line: list[Optional[float]] = [None] * len(lines)
    matcher = SequenceMatcher(None, lyric_toks, whisper_toks, autojunk=False)
    for li, wi, size in matcher.get_matching_blocks():
        for k in range(size):
            line_no = owners[li + k]
            t = starts[wi + k]
            if per_line[line_no] is None or t < per_line[line_no]:  # type: ignore[operator]
                per_line[line_no] = t

    per_line = _drop_impossible_anchors(per_line, weights)
    return list(zip(_interpolate(per_line, horizon, weights), lines))


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
