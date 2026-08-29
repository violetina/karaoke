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


def lines_to_lrc(lines: list[tuple[float, str]]) -> str:
    """Render (time, text) lines to LRC text ([mm:ss.xx] line)."""
    out = []
    for t, text in lines:
        mm = int(t // 60)
        ss = t - mm * 60
        out.append(f"[{mm:02d}:{ss:05.2f}] {text}")
    return "\n".join(out)


def transcribe_to_lrc(
    audio_path: str,
    *,
    model_size: str = "base",
    language: Optional[str] = None,
    compute_type: str = "int8",
) -> str:
    """Transcribe an audio file to LRC text using faster-whisper.

    Returns LRC string (may be empty if nothing was transcribed).
    """
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cpu", compute_type=compute_type)
    segments, _info = model.transcribe(
        audio_path, word_timestamps=True, language=language,
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
    lines = group_words_to_lines(words)
    return lines_to_lrc(lines)


def _cli() -> int:  # pragma: no cover - manual run
    import argparse

    ap = argparse.ArgumentParser(description="Transcribe audio -> LRC via Whisper")
    ap.add_argument("audio")
    ap.add_argument("--model", default="base")
    ap.add_argument("--language", default=None)
    a = ap.parse_args()
    print(transcribe_to_lrc(a.audio, model_size=a.model, language=a.language))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
