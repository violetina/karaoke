"""Audio tag extraction via mutagen."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac"}


@dataclass
class TrackTags:
    """Normalized metadata extracted from one local audio file."""

    path: str
    title: str
    artist: str
    album: str = ""
    year: Optional[int] = None
    duration: Optional[float] = None


def _first(d: dict, key: str) -> str:
    v = d.get(key)
    if isinstance(v, (list, tuple)):
        return str(v[0]) if v else ""
    return str(v) if v else ""


def _year(raw: str) -> Optional[int]:
    for tok in (raw or "").replace("/", "-").split("-"):
        tok = tok.strip()
        if len(tok) >= 4 and tok[:4].isdigit():
            return int(tok[:4])
    return None


def extract_tags(path: str | Path) -> TrackTags:
    """Read tags from an audio file. Falls back to the filename for title."""
    import mutagen  # imported lazily so config/tests don't need the dep

    p = Path(path)
    easy = mutagen.File(str(p), easy=True)  # type: ignore[attr-defined]
    title = artist = album = ""
    year = None
    duration = None
    if easy is not None:
        title = _first(easy, "title")
        artist = _first(easy, "artist")
        album = _first(easy, "album")
        year = _year(_first(easy, "date") or _first(easy, "year"))
        if getattr(easy, "info", None) is not None:
            duration = float(getattr(easy.info, "length", 0.0)) or None
    if not title:
        title = p.stem
    return TrackTags(
        path=str(p),
        title=title,
        artist=artist,
        album=album,
        year=year,
        duration=duration,
    )


def is_audio(path: str | Path) -> bool:
    """Return true when `path` has a supported audio-file extension."""
    return Path(path).suffix.lower() in AUDIO_EXTS
