"""YouTube mode: resolve a SongRef from a YouTube URL.

YouTube video titles are much noisier than streaming metadata (which
``lyrics.clean_title`` already handles): they carry promo decorations like
``(Official Music Video)``, ``[Lyrics]``, ``| Records``, VEVO/uploader noise and
inconsistent ``Artist - Title`` ordering. This module turns that mess into a best
guess of ``(artist, title)`` so the existing LRCLIB -> cache -> synced-render
pipeline can take over unchanged.

Two layers:

- ``parse_youtube_title`` — a *pure* (no network) heuristic that strips YouTube
  promo decorations and splits artist/title. Unit-tested against real-world junk.
- ``fetch_metadata`` / ``resolve_youtube`` — thin yt-dlp wrappers that pull the
  video title/uploader/duration (and optionally download the audio so the Whisper
  fallback and librosa beat detection can run, which Spotify mode can never do).

yt-dlp is an *optional* dependency (``pip install 'karaoke[youtube]'``); the
import is lazy so the rest of the app never pays for it.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .config import settings
from .identify import SongRef

# Bracketed/parenthesised groups are dropped when they contain any of these
# promo keywords. We deliberately keep groups like "(feat. X)" or "(Remastered
# 2011)" — LRCLIB's fetch already retries with lyrics.clean_title(), which knows
# those — so removing them here would only lose signal.
_JUNK_WORDS = (
    r"official|officiel|video|videoclip|vídeo|audio|lyrics?|"
    r"visuali[sz]er|lyric\s*video|music\s*video|m/?v|hd|hq|4k|8k|"
    r"full\s*album|full\s*video|explicit|clean|clip\s*officiel|"
    r"color\s*coded|colou?r\s*coded|sub\s*espa|legendado|tradu|"
    r"live\s*performance|performance\s*video|dance\s*(?:practice|version)|"
    r"remix\s*video|cover|karaoke\s*version"
)
_DECORATION = re.compile(
    r"\s*[\(\[\{][^\)\]\}]*(?:%s)[^\)\]\}]*[\)\]\}]" % _JUNK_WORDS,
    re.IGNORECASE,
)

# Standalone (unbracketed) trailing promo phrases, e.g.
# "Artist - Title Official Music Video". We require a qualifier (official / music
# / lyric) or an explicit "... Lyrics" so we never strip a lone real word like
# the trailing "Video" in "Video Games" or "Audio".
_BARE_EDGE = re.compile(
    r"\s+(?:"
    r"official(?:\s+(?:music|lyric))?\s+(?:video|audio)"
    r"|(?:music|lyric)\s+video"
    r"|official\s+audio"
    r"|lyrics?"
    r")\s*$",
    re.IGNORECASE,
)

# Everything after a trailing " | ..." (label / channel tag) is noise.
_TRAILING_PIPE = re.compile(r"\s*\|.*$")

# Uploader suffixes that are not part of the artist name.
_UPLOADER_TAIL = re.compile(
    r"\s*(?:-\s*topic|vevo|official|officiel|music|records?|tv|channel|"
    r"entertainment)\s*$",
    re.IGNORECASE,
)

_DASHES = (" - ", " – ", " — ", " ~ ")


def _strip_decorations(title: str) -> str:
    """Remove YouTube promo decorations from a raw video title."""
    out = title.strip()
    # Drop everything after a trailing pipe (label/tag), then bracketed junk.
    out = _TRAILING_PIPE.sub("", out).strip()
    prev = None
    while out and out != prev:  # collapse stacked decorations
        prev = out
        out = _DECORATION.sub("", out).strip()
        out = _BARE_EDGE.sub("", out).strip()
    # Tidy leftover separators / quotes / whitespace.
    out = out.strip(" -–—|·•\t")
    out = re.sub(r"\s{2,}", " ", out)
    return out or title.strip()


def clean_uploader(uploader: str) -> str:
    """Normalise a channel/uploader name into a plausible artist name."""
    out = (uploader or "").strip()
    prev = None
    while out and out != prev:
        prev = out
        out = _UPLOADER_TAIL.sub("", out).strip()
    return out or (uploader or "").strip()


def _split_artist_title(text: str) -> tuple[str, str]:
    """Split "Artist - Title" on the first dash-like separator.

    Returns ("", text) when there is no usable split (both sides must be
    non-empty after stripping quotes/whitespace).
    """
    for sep in _DASHES:
        if sep in text:
            left, right = text.split(sep, 1)
            left = left.strip().strip('"\u201c\u201d\u2018\u2019')
            right = right.strip().strip('"\u201c\u201d\u2018\u2019')
            if left and right:
                return left, right
    return "", text.strip()


def parse_youtube_title(
    raw_title: str, uploader: Optional[str] = None
) -> tuple[str, str]:
    """Best-guess ``(artist, title)`` from a YouTube video title + uploader.

    Heuristics, in order:

    1. Strip promo decorations (``(Official Video)``, ``[Lyrics]``, ``| Label``…).
    2. Auto-generated "Art - Topic" channels: the uploader IS the artist. Prefer
       an explicit "Artist - Title" split in the (usually clean) title, else use
       the Topic artist with the whole title.
    3. A normal "Artist - Title" split in the cleaned title.
    4. Fall back to the cleaned uploader as artist + cleaned title.
    """
    cleaned = _strip_decorations(raw_title)
    up = (uploader or "").strip()

    if up.lower().endswith("- topic") or up.lower().endswith("topic"):
        topic_artist = clean_uploader(up)
        a, t = _split_artist_title(cleaned)
        if a and t:
            return a, t
        return topic_artist, cleaned

    a, t = _split_artist_title(cleaned)
    if a and t:
        return a, t

    if up:
        return clean_uploader(up), cleaned
    return "", cleaned


def fetch_metadata(url: str, *, download: bool = False) -> dict:
    """Fetch YouTube video metadata via yt-dlp (optionally downloading audio).

    Returns a dict with ``title``, ``uploader``, ``duration`` (seconds, float or
    None) and ``path`` (local audio file when ``download=True``, else None).

    Raises ``RuntimeError`` with an actionable message when yt-dlp is not
    installed, so the CLI can tell the user how to enable YouTube mode.
    """
    try:
        from yt_dlp import YoutubeDL  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise RuntimeError(
            "YouTube mode needs yt-dlp. Install it with "
            "`pip install 'karaoke[youtube]'` (or `pip install yt-dlp`)."
        ) from exc

    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": not download,
    }
    path: Optional[str] = None
    if download:
        out_dir = Path(settings.data_dir) / "youtube"
        out_dir.mkdir(parents=True, exist_ok=True)
        opts.update(
            {
                "format": "bestaudio/best",
                "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
            }
        )

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=download)
        if download:
            path = ydl.prepare_filename(info)

    dur = info.get("duration")
    return {
        "title": info.get("track") or info.get("title") or "",
        "uploader": info.get("artist") or info.get("uploader") or "",
        "duration": float(dur) if dur is not None else None,
        "path": path if (path and Path(path).is_file()) else None,
    }


def resolve_youtube(url: str, *, download: bool = False) -> Optional[SongRef]:
    """Resolve a YouTube URL into a SongRef (source="youtube").

    Uses yt-dlp track/artist tags when present (music.youtube.com often supplies
    them), otherwise the smart title parser. Returns None only when yt-dlp yields
    no usable title at all.
    """
    meta = fetch_metadata(url, download=download)
    raw_title = meta["title"]
    if not raw_title:
        return None

    # music.youtube.com may already give clean track/artist tags.
    if meta["uploader"] and not any(s in raw_title for s in _DASHES) and (
        meta["uploader"].lower().endswith(("- topic", "topic"))
    ):
        artist = clean_uploader(meta["uploader"])
        title = _strip_decorations(raw_title)
    else:
        artist, title = parse_youtube_title(raw_title, meta["uploader"])

    if not title:
        return None
    return SongRef(
        artist=artist,
        title=title,
        duration=meta["duration"],
        path=meta["path"],
        source="youtube",
    )
