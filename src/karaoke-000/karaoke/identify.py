"""Song identification: resolve a track from a file, a text query, or live audio.

Live identification reuses songrec (the same engine behind the `whatsong` tool):
it fingerprints microphone or monitor audio against Shazam and returns artist +
title, which we then use to look up lyrics.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .tags import extract_tags


@dataclass
class SongRef:
    artist: str
    title: str
    album: str = ""
    duration: Optional[float] = None
    path: Optional[str] = None       # local file, if any
    source: str = "unknown"          # file | query | songrec | index


def from_file(path: str | Path) -> SongRef:
    t = extract_tags(path)
    return SongRef(
        artist=t.artist, title=t.title, album=t.album,
        duration=t.duration, path=t.path, source="file",
    )


def parse_query(text: str) -> SongRef:
    """Parse 'Artist - Title' (or just a title) into a SongRef."""
    if " - " in text:
        artist, title = text.split(" - ", 1)
        return SongRef(artist=artist.strip(), title=title.strip(), source="query")
    return SongRef(artist="", title=text.strip(), source="query")


def _default_source(mic: bool) -> Optional[str]:
    """Resolve the pactl source name: mic (default) or output monitor."""
    if not shutil.which("pactl"):
        return None
    try:
        if mic:
            out = subprocess.run(["pactl", "get-default-source"],
                                 capture_output=True, text=True, timeout=5)
            name = out.stdout.strip()
            return name or None
        out = subprocess.run(["pactl", "get-default-sink"],
                             capture_output=True, text=True, timeout=5)
        sink = out.stdout.strip()
        return f"{sink}.monitor" if sink else None
    except (subprocess.SubprocessError, OSError):
        return None


def identify_live(mic: bool = True, timeout: int = 30) -> Optional[SongRef]:
    """Listen and identify the currently playing song via songrec.

    mic=True listens to the microphone (room audio); mic=False uses the
    output sink monitor (audio playing through this machine).
    Returns None if nothing is recognized.
    """
    if not shutil.which("songrec"):
        raise RuntimeError("songrec not installed (emerge media-sound/songrec)")
    src = _default_source(mic)
    cmd = ["songrec", "recognize", "-j"]
    if src:
        cmd = ["songrec", "recognize", "-d", src, "-j"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    out = proc.stdout.strip()
    if not out:
        return None
    try:
        data = json.loads(out)
        track = data["track"]
    except (json.JSONDecodeError, KeyError):
        return None
    return SongRef(
        artist=track.get("subtitle", ""),
        title=track.get("title", ""),
        source="songrec",
    )
