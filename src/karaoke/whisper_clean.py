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
from difflib import SequenceMatcher
from typing import Iterable, Optional

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


_NON_WORD = re.compile(r"[^\w\s]")

# Real spoken promo tags are brief -- a handful of seconds naming the
# artist/album/title before the song starts. Capping how far into the track
# this is allowed to reach keeps the filter from ever eating real lyrics just
# because a line happens to echo the track's own title (choruses do that).
_MAX_METADATA_INTRO_S = 15.0


def _normalize_for_match(text: str) -> str:
    return _NON_WORD.sub("", (text or "").strip().lower())


def _is_metadata_line(text: str, metadata_norms: list[str]) -> bool:
    norm = _normalize_for_match(text)
    if not norm:
        return False
    for m in metadata_norms:
        if not m:
            continue
        if m in norm or norm in m:
            return True
        if SequenceMatcher(None, norm, m).ratio() >= 0.7:
            return True
    return False


def drop_spoken_metadata_intro(
    lines: list[tuple[float, str]],
    artist: Optional[str] = None,
    title: Optional[str] = None,
    album: Optional[str] = None,
    max_intro_s: float = _MAX_METADATA_INTRO_S,
) -> list[tuple[float, str]]:
    """Drop a leading run of lines that just speak the track's own metadata.

    Some audio files (promo samples, certain CD rips) open with a spoken
    voice-over announcing the artist/album/title before the song starts --
    faster-whisper transcribes it faithfully (it is real speech), but it is
    never genuine lyric content: no song opens by reciting its own metadata,
    one field per line. This only matters where Whisper is the *only* source
    of words (the ``source="whisper"`` full-transcription fallback with no
    known-good lyrics text to align against) -- found live on Morphine's "I
    Know You (Pt. III)", whose first three "lyric" lines were literally
    "Morphine" / "Like Swimming" / "I Know You (Pt. III)", which silently
    threw off every real line's sync by several seconds.

    Only strips a genuine PREFIX run (stops at the first non-matching line),
    and only when that whole run sits within ``max_intro_s`` of the start --
    a long or late match is more likely a coincidental lyric than a promo
    tag, so it is left alone rather than risking real content.
    """
    metadata_norms = [_normalize_for_match(m) for m in (artist, title, album) if m and m.strip()]
    if not metadata_norms or not lines:
        return lines

    cut = 0
    for t, text in lines:
        if t > max_intro_s or not _is_metadata_line(text, metadata_norms):
            break
        cut += 1
    return lines[cut:] if cut else lines
