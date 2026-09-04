"""Cover art as coloured terminal cells.

The player already has the artwork: Chromium writes YouTube Music's cover to a
local file and advertises it over MPRIS as ``mpris:artUrl``, so nothing has to
be downloaded. When there is no art but the track's audio is in the YouTube
cache, the first video frame stands in.

Decoding is delegated to ffmpeg, which is already required for downloads. That
avoids adding an image library and, more usefully, handles whatever the source
happens to be — PNG from MPRIS, JPEG or WebP from a thumbnail — without caring.

Each pixel is drawn as a SPACE with a background colour. A space is
unambiguously one cell wide, so unlike block or half-block characters the art
cannot shift by a column on a terminal that draws those glyphs wide.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

# Terminal cells are roughly twice as tall as they are wide, so a square cover
# needs half as many rows as columns to keep its proportions.
CELL_ASPECT = 2


def art_path_from_url(art_url: str) -> Optional[Path]:
    """Local path for an MPRIS ``mpris:artUrl``, if it points at a real file."""
    if not art_url:
        return None
    parsed = urlparse(art_url)
    if parsed.scheme not in ("file", ""):
        return None                      # remote art is not fetched here
    path = Path(unquote(parsed.path or art_url))
    return path if path.is_file() else None


def sample(path: Path, cols: int, rows: int,
           *, timeout: float = 5.0) -> Optional[list[list[tuple[int, int, int]]]]:
    """Decode and downscale an image (or a video's first frame) to RGB cells.

    Returns rows of (r, g, b), or None if ffmpeg cannot read the source.
    """
    if cols < 1 or rows < 1:
        return None
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path),
             # -frames:v 1 makes this work on a video file too, taking frame one.
             "-frames:v", "1",
             "-vf", f"scale={cols}:{rows}",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = proc.stdout
    if len(raw) < cols * rows * 3:
        return None
    out = []
    for y in range(rows):
        line = []
        for x in range(cols):
            i = (y * cols + x) * 3
            line.append((raw[i], raw[i + 1], raw[i + 2]))
        out.append(line)
    return out


def to_text(pixels: list[list[tuple[int, int, int]]]):
    """Render sampled pixels as a Rich Text of background-coloured spaces."""
    from rich.text import Text

    text = Text(no_wrap=True, overflow="crop")
    for y, row in enumerate(pixels):
        for (r, g, b) in row:
            text.append(" ", style=f"on rgb({r},{g},{b})")
        if y < len(pixels) - 1:
            text.append("\n")
    return text


def render(source: Path, cols: int, rows: Optional[int] = None):
    """Cover art sized for a panel, or None when it cannot be rendered."""
    if rows is None:
        rows = max(1, cols // CELL_ASPECT)
    pixels = sample(source, cols, rows)
    return to_text(pixels) if pixels else None
