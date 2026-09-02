"""Detect YouTube caption availability and convert captions into synced LRC.

YouTube exposes two distinct caption sets via yt-dlp:

- ``subtitles`` — captions the uploader supplied. For music videos these are
  frequently the actual lyrics, so they are the highest-quality source.
- ``automatic_captions`` — ASR transcripts. Usable, but noisier.

Both can be requested in the ``json3`` format, which carries per-word
millisecond offsets. That is finer-grained than LRC needs, so words are grouped
back into their caption cue and each cue becomes one timed LRC line. This gives
synced lyrics without running Whisper.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

# Sound-effect / non-lyric cues that captions insert. Matched anywhere in a
# line, because real caption cues embed them mid-text ("[music] Heat. Heat.").
_NOISE_TOKEN_RE = re.compile(
    r"\[\s*(music|musik|muziek|applause|laughter|cheering|silence|sound"
    r"|instrumental|singing|humming|vocalizing|no speech|inaudible)\s*\]",
    re.IGNORECASE,
)
# Caption speaker/turn markers, e.g. ">> Heat".
_SPEAKER_RE = re.compile(r"^\s*(?:>>+|-\s)\s*")
# Real caption languages, e.g. en, en-orig, en-GB, nl. Excludes yt-dlp pseudo
# languages such as `en-ehkg1hFWq8A` (a video-id suffix) and `live_chat`.
_LANG_RE = re.compile(r"^[a-z]{2,3}(?:-(?:orig|[A-Z]{2}|Latn|Hans|Hant))?$")

# json3 first: it is the only format carrying per-word timing.
_EXT_PREFERENCE = ("json3", "srv3", "vtt", "ttml", "srt")


@dataclass(frozen=True)
class CaptionTrack:
    """A single selectable caption track."""

    language: str
    ext: str
    url: str
    kind: str  # "manual" | "automatic"


@dataclass(frozen=True)
class CaptionAvailability:
    """What captions a video offers, and the best track to use."""

    has_manual: bool
    has_automatic: bool
    manual_languages: tuple[str, ...]
    automatic_languages: tuple[str, ...]
    best: Optional[CaptionTrack]

    @property
    def available(self) -> bool:
        """True when any usable caption track was found."""
        return self.best is not None

    def describe(self) -> str:
        """Human-readable one-line summary for CLI output."""
        if not self.available:
            return "no captions"
        assert self.best is not None
        return f"{self.best.kind} captions [{self.best.language}] as {self.best.ext}"


def is_real_language(code: str) -> bool:
    """Return True for genuine caption language codes.

    Filters yt-dlp artifacts: ``live_chat`` is not a caption track, and
    per-video pseudo-languages like ``en-ehkg1hFWq8A`` are not selectable.
    """
    if not code or code == "live_chat":
        return False
    return bool(_LANG_RE.match(code))


def _pick_ext(entries: list[dict]) -> Optional[dict]:
    """Choose the richest caption format available for one language."""
    for ext in _EXT_PREFERENCE:
        for entry in entries:
            if entry.get("ext") == ext and entry.get("url"):
                return entry
    return None


def probe_captions(
    info: Any,
    languages: Iterable[str] = ("en", "en-orig", "nl"),
) -> CaptionAvailability:
    """Inspect a yt-dlp info dict and report caption availability.

    Manual (uploader-provided) captions outrank automatic ones because for
    music videos they are typically the real lyrics.
    """
    info = info or {}
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if is_real_language(k)}
    automatic = {
        k: v for k, v in (info.get("automatic_captions") or {}).items() if is_real_language(k)
    }

    preferred = list(languages)
    # Consider requested languages first, then any other real language.
    ordered = preferred + [k for k in (*manual, *automatic) if k not in preferred]

    best: Optional[CaptionTrack] = None
    for kind, pool in (("manual", manual), ("automatic", automatic)):
        for lang in ordered:
            entry = _pick_ext(pool.get(lang) or [])
            if entry:
                best = CaptionTrack(
                    language=lang, ext=entry["ext"], url=entry["url"], kind=kind
                )
                break
        if best:
            break

    return CaptionAvailability(
        has_manual=bool(manual),
        has_automatic=bool(automatic),
        manual_languages=tuple(manual),
        automatic_languages=tuple(automatic),
        best=best,
    )


def _format_lrc_timestamp(ms: int) -> str:
    """Render milliseconds as ``[mm:ss.cc]``.

    LRC has no hour field, so anything past 60 minutes keeps counting minutes
    (1h02m03s becomes ``62:03``).
    """
    total_cs = max(0, int(round(ms / 10.0)))
    minutes, rem_cs = divmod(total_cs, 6000)
    seconds, centis = divmod(rem_cs, 100)
    return f"[{minutes:02d}:{seconds:02d}.{centis:02d}]"


def _format_word_timestamp(ms: int) -> str:
    """Render milliseconds as an Enhanced LRC word tag ``<mm:ss.cc>``."""
    return "<" + _format_lrc_timestamp(ms)[1:-1] + ">"


def _cue_text(event: dict) -> str:
    """Join a cue's word segments into a single cleaned line.

    Strips speaker markers and embedded sound-effect tokens; real caption cues
    mix them into lyric text rather than isolating them on their own line.
    """
    parts = [str(seg.get("utf8", "")) for seg in event.get("segs") or []]
    text = "".join(parts)
    text = text.replace("\u200b", " ")
    text = _NOISE_TOKEN_RE.sub(" ", text)
    text = _SPEAKER_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_token(raw: str) -> str:
    """Normalise one caption word segment."""
    text = str(raw).replace("\u200b", " ")
    text = _NOISE_TOKEN_RE.sub(" ", text)
    return text


def _split_event(
    event: dict, start_ms: int, max_words: int
) -> list[tuple[int, str, list[int]]]:
    """Turn one cue into timed lines, splitting long cues on word offsets.

    json3 gives each word a ``tOffsetMs`` relative to the cue start, so a long
    cue can be broken into shorter karaoke lines at genuine word times rather
    than interpolated guesses. Returns ``(start_ms, text, word_start_ms)`` so
    callers can emit either plain or Enhanced LRC.
    """
    words: list[tuple[int, str]] = []
    for seg in event.get("segs") or []:
        token = _clean_token(seg.get("utf8", ""))
        if not token.strip():
            continue
        offset = seg.get("tOffsetMs") or 0
        words.append((start_ms + int(offset), token))

    if not words:
        return []

    # Reassemble, keeping each word's own timestamp as a potential line start.
    merged: list[tuple[int, str]] = []
    for ts, token in words:
        if merged and not token.startswith(" ") and not merged[-1][1].endswith(" "):
            # Continuation of the previous word (json3 splits mid-word).
            merged[-1] = (merged[-1][0], merged[-1][1] + token)
        else:
            merged.append((ts, token))

    cleaned = [(ts, tok.strip()) for ts, tok in merged if tok.strip()]
    if not cleaned:
        return []

    if max_words <= 0 or len(cleaned) <= max_words:
        chunks = [cleaned]
    else:
        chunks = [
            cleaned[i : i + max_words]
            for i in range(0, len(cleaned), max_words)
        ]

    out: list[tuple[int, str, list[int]]] = []
    for chunk in chunks:
        text = _SPEAKER_RE.sub("", " ".join(t for _, t in chunk)).strip()
        if not text:
            continue
        # Speaker-marker stripping can drop leading tokens; keep one timestamp
        # per surviving word so word tags line up with the text.
        times = [ts for ts, _ in chunk][-len(text.split()):]
        out.append((chunk[0][0], text, times))
    return out


def _caption_rows(
    payload: str | dict, max_words: int
) -> list[tuple[int, str, list[int]]]:
    """Parse a json3 payload into deduped ``(start_ms, text, word_ms)`` rows.

    Shared by the plain and Enhanced LRC writers so both agree exactly on line
    splitting, ordering and de-duplication.

    Raises ``ValueError`` when the payload is not valid json3.
    """
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid json3 caption payload: {exc}") from exc
    else:
        data = payload
    if not isinstance(data, dict):
        raise ValueError("json3 caption payload must be an object")

    rows: list[tuple[int, str, list[int]]] = []
    for event in data.get("events") or []:
        if not isinstance(event, dict) or not event.get("segs"):
            continue
        start = event.get("tStartMs")
        if start is None:
            continue
        rows.extend(_split_event(event, int(start), max_words))

    rows.sort(key=lambda r: r[0])

    deduped: list[tuple[int, str, list[int]]] = []
    previous: Optional[str] = None
    for start_ms, text, word_ms in rows:
        # Rolling auto-captions repeat the same text as it grows; keep changes.
        if text == previous:
            continue
        previous = text
        deduped.append((start_ms, text, word_ms))
    return deduped


def json3_to_lrc(payload: str | dict, *, max_words: int = 10) -> str:
    """Convert a YouTube ``json3`` caption payload into LRC text.

    Each caption cue becomes one or more timed lines. Cues longer than
    ``max_words`` are split on the per-word ``tOffsetMs`` timings json3
    provides, so long uploader-supplied cues stay readable on screen without
    inventing timestamps. Pass ``max_words=0`` to disable splitting.

    Raises ``ValueError`` when the payload is not valid json3.
    """
    return "\n".join(
        f"{_format_lrc_timestamp(start_ms)}{text}"
        for start_ms, text, _ in _caption_rows(payload, max_words)
    )


def json3_to_enhanced_lrc(payload: str | dict, *, max_words: int = 10) -> str:
    """Convert a ``json3`` caption payload into **Enhanced** LRC.

    json3 carries a start time for every word, so the output keeps them as
    ``<mm:ss.xx>`` word tags:

    ``[00:12.00]<00:12.00>I <00:12.30>see <00:12.60>trees``

    That gives real word-level highlighting with no interpolation and no
    manual tapping. Per the Enhanced LRC convention the first word tag matches
    the line timestamp. Falls back to a bare line stamp when a row somehow has
    no word timings.

    Raises ``ValueError`` when the payload is not valid json3.
    """
    out: list[str] = []
    for start_ms, text, word_ms in _caption_rows(payload, max_words):
        stamp = _format_lrc_timestamp(start_ms)
        words = text.split()
        if len(word_ms) != len(words):
            out.append(f"{stamp}{text}")
            continue
        tagged = " ".join(
            f"{_format_word_timestamp(ts)}{w}" for ts, w in zip(word_ms, words)
        )
        out.append(f"{stamp}{tagged}")
    return "\n".join(out)
