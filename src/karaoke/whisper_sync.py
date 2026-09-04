"""Whisper fallback: transcribe local audio to approximate synced (LRC) lyrics.

Used when LRCLIB has no synced lyrics for a track but a local audio file exists.
Uses faster-whisper with word timestamps, then groups words into readable lines
by pause length and max line length. Output is imperfect but good enough to sing
along; results are cached in OpenSearch so transcription runs at most once.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

# Tunables for grouping words -> lyric lines.
_PAUSE_SPLIT = 0.8      # seconds of silence between words that forces a new line
_MAX_CHARS = 42         # soft cap on line length
_MAX_WORDS = 10         # hard cap on words per line


@dataclass
class Word:
    """One word timestamp emitted by faster-whisper."""

    start: float
    end: float
    text: str


def group_words_to_lines(
    words: Iterable[Word],
    *,
    pause_split: float = _PAUSE_SPLIT,
    max_chars: int = _MAX_CHARS,
    max_words: int = _MAX_WORDS,
) -> list[tuple[float, str]]:
    """Group timestamped words into (start_time, line_text) tuples.

    A new line starts when: the gap since the previous word exceeds
    `pause_split`, the current line would exceed `max_chars`, or `max_words`
    is reached. Line timestamp = start time of its first word.
    """
    lines: list[tuple[float, str]] = []
    cur: list[str] = []
    cur_start: Optional[float] = None
    prev_end: Optional[float] = None

    def flush() -> None:
        nonlocal cur, cur_start
        if cur and cur_start is not None:
            lines.append((round(cur_start, 2), " ".join(cur).strip()))
        cur = []
        cur_start = None

    for w in words:
        text = w.text.strip()
        if not text:
            continue
        gap = (w.start - prev_end) if prev_end is not None else 0.0
        would_len = len(" ".join(cur + [text]))
        if cur and (gap >= pause_split or would_len > max_chars or len(cur) >= max_words):
            flush()
        if cur_start is None:
            cur_start = w.start
        cur.append(text)
        prev_end = w.end
    flush()
    return lines


def _dedup_adjacent(
    lines: list[tuple[float, str]],
    *,
    max_gap: float = 6.0,
) -> list[tuple[float, str]]:
    """Drop a line whose text repeats the immediately previous line within
    `max_gap` seconds. Kills Whisper's repetition artifact on music beds while
    preserving intentional repeats (chorus lines spaced further apart).
    """
    out: list[tuple[float, str]] = []
    for t, text in lines:
        norm = text.strip().lower()
        if out:
            pt, ptext = out[-1]
            if norm == ptext.strip().lower() and (t - pt) <= max_gap:
                continue
        out.append((t, text))
    return out


def lines_to_lrc(lines: list[tuple[float, str]]) -> str:
    """Render (time, text) lines to LRC text ([mm:ss.xx] line)."""
    out = []
    for t, text in lines:
        mm = int(t // 60)
        ss = t - mm * 60
        out.append(f"[{mm:02d}:{ss:05.2f}] {text}")
    return "\n".join(out)


def transcribe_to_words(
    audio_path: str,
    *,
    text: Optional[str] = None,
    model_size: str = "small",
    language: Optional[str] = None,
    compute_type: str = "int8",
    vad_filter: bool = False,
) -> list[Word]:
    """Transcribe an audio file to timestamped words using faster-whisper.

    Defaults to the `small` model. `condition_on_previous_text=False`
    suppresses the line-repetition the model produces on musical audio. VAD is
    OFF by default: faster-whisper's Silero VAD over-filters singing/quiet
    intros and can discard the entire track.

    Word timings are the useful product — Whisper's *words* on music are
    unreliable, but its *rhythm* is good. Callers that hold the real lyrics can
    align them onto these timings; see :mod:`karaoke.lyric_align`.
    """
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cpu", compute_type=compute_type)
    segments, _info = model.transcribe(
        audio_path,
        initial_prompt=text,
        word_timestamps=True,
        language=language,
        vad_filter=vad_filter,
        # Suppress runaway repetition (Whisper looping a line on music beds).
        condition_on_previous_text=False,
    )
    words: list[Word] = []
    for seg in segments:
        seg_words = getattr(seg, "words", None)
        if seg_words:
            for w in seg_words:
                words.append(Word(start=w.start, end=w.end, text=w.word))
        else:
            # no word timestamps -> fall back to one line per segment
            words.append(Word(start=seg.start, end=seg.end, text=seg.text))
    return words


def transcribe_to_lrc(
    audio_path: str,
    *,
    text: Optional[str] = None,
    model_size: str = "small",
    language: Optional[str] = None,
    compute_type: str = "int8",
    vad_filter: bool = False,
) -> str:
    """Transcribe an audio file to LRC text using faster-whisper.

    Groups :func:`transcribe_to_words` output into lines and renders LRC. A
    post-hoc dedup pass drops adjacent repeats. Returns an LRC string (may be
    empty if nothing was transcribed).
    """
    words = transcribe_to_words(
        audio_path, text=text, model_size=model_size, language=language,
        compute_type=compute_type, vad_filter=vad_filter,
    )
    lines = _dedup_adjacent(group_words_to_lines(words))
    return lines_to_lrc(lines)


def _cli() -> int:  # pragma: no cover - manual run
    import argparse

    ap = argparse.ArgumentParser(description="Transcribe audio -> LRC via Whisper")
    ap.add_argument("audio")
    ap.add_argument("--model", default="small")
    ap.add_argument("--language", default=None)
    ap.add_argument("--vad", action="store_true",
                    help="enable VAD filtering (off by default; can over-filter singing)")
    a = ap.parse_args()
    print(transcribe_to_lrc(a.audio, model_size=a.model, language=a.language,
                            vad_filter=a.vad))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
