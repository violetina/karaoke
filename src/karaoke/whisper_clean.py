"""Filter the artifacts out of a Whisper transcription.

Needed most where Whisper is the *only* source of words. When real lyrics
exist they can corroborate each token (see ``lyric_align.trust_word``), but a
track no lyric service carries has nothing to check against -- and that is
exactly the case this was written for.

Everything here is a pure function over lines or tokens, so it is testable
without audio.
"""
from __future__ import annotations

import re
from typing import Iterable

# Non-lexical leftovers: the musical-note glyphs Whisper emits over
# instrumentals, bracketed stage directions, and bare punctuation.
_ARTIFACT_LINE = re.compile(
    r"^\s*(?:[\U0001F300-\U0001FAFF\u2600-\u27BF\s]+|"
    r"[\[\(](?:music|applause|silence|instrumental|inaudible)[^\]\)]*[\]\)]|"
    r"[^\w\s]+)\s*$",
    re.IGNORECASE,
)

# How many identical lines in a row before it is a stuck model rather than a
# chorus. Three is deliberate: two is a real lyric device ("hey, hey"), and
# runaway loops produce far more than three.
MAX_REPEATS = 3

# A "word" of one or two letters repeated with no vowels is not language.
_NO_VOWEL = re.compile(r"^[^aeiouy]+$", re.IGNORECASE)


def is_artifact(line: str) -> bool:
    """Whether a line carries no lyric content at all."""
    stripped = (line or "").strip()
    if not stripped:
        return True
    return bool(_ARTIFACT_LINE.match(stripped))


def drop_runaway_repeats(lines: Iterable[str],
                         max_repeats: int = MAX_REPEATS) -> list[str]:
    """Collapse a line repeated past the point of being a chorus.

    Whisper on a music bed gets stuck and emits the same phrase dozens of
    times. A song does repeat a line -- so the cut is at ``max_repeats``
    consecutive identical lines, which no lyric does and every loop exceeds.
    """
    out: list[str] = []
    run_of = None
    count = 0
    for line in lines:
        key = line.strip().casefold()
        if key and key == run_of:
            count += 1
            if count > max_repeats:
                continue
        else:
            run_of, count = key, 1
        out.append(line)
    return out


def looks_like_a_word(token: str) -> bool:
    """Whether a token is plausibly a word rather than transcription noise."""
    tok = (token or "").strip()
    if len(tok) < 2:
        # "a" and "I" are the only real one-letter words.
        return tok.lower() in {"a", "i"}
    return not _NO_VOWEL.match(tok)


def clean_lines(lines: Iterable[str]) -> list[str]:
    """Remove artifacts and runaway repeats from transcribed lines."""
    kept = [ln for ln in lines if not is_artifact(ln)]
    return drop_runaway_repeats(kept)


def clean_text(text: str) -> str:
    """Filter a whole transcription."""
    return "\n".join(clean_lines((text or "").splitlines()))
