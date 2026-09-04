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

import os
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

# How many times taller than wide a terminal cell is. A square image needs
# `cols / CELL_ASPECT` rows to still look square.
#
# 2.0 is the textbook figure, but it depends on the font and on any line
# spacing the terminal adds — with extra leading, cells are taller and art
# rendered for 2.0 comes out stretched. 2.5 is what a real terminal here
# measured out at, calibrated by eye against `python -m karaoke.coverart`, and
# is a better default than the theoretical value.
#
# There is no way to query this from inside a TUI, so it stays tunable: raise
# it if the cover still looks too tall, lower it if it looks squashed.
try:
    CELL_ASPECT = float(os.environ.get("KARAOKE_CELL_ASPECT", "2.5"))
except ValueError:
    CELL_ASPECT = 2.5


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


def probe_size(path: Path, *, timeout: float = 5.0) -> Optional[tuple[int, int]]:
    """Pixel dimensions of an image or video stream, via ffprobe."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    parts = proc.stdout.strip().split(",")
    try:
        width, height = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def fit(src: tuple[int, int], max_cols: int,
        max_rows: int) -> Optional[tuple[int, int]]:
    """Cell dimensions that preserve the source's aspect within the panel.

    Terminal cells are about twice as tall as they are wide, so a square image
    needs half as many rows as columns to still look square. Width leads;
    height follows from the aspect and is then clamped, with the width reduced
    to match rather than letting the image stretch.
    """
    src_w, src_h = src
    if src_w <= 0 or src_h <= 0 or max_cols < 1 or max_rows < 1:
        return None
    cols = max_cols
    rows = max(1, round(cols * (src_h / src_w) / CELL_ASPECT))
    if rows > max_rows:
        rows = max_rows
        cols = max(1, round(rows * CELL_ASPECT * (src_w / src_h)))
        cols = min(cols, max_cols)
    return cols, rows


def _calibration() -> str:  # pragma: no cover - manual visual check
    """Print a square, to calibrate KARAOKE_CELL_ASPECT by eye.

    If the block below looks taller than it is wide, raise the value; if it
    looks squat, lower it. Run as `python -m karaoke.coverart`.
    """
    cols = 20
    rows = max(1, round(cols / CELL_ASPECT))
    block = "\n".join("#" * cols for _ in range(rows))
    return (f"KARAOKE_CELL_ASPECT={CELL_ASPECT}  ->  {cols}x{rows} cells\n"
            f"{block}\n"
            "This should look SQUARE. Taller than wide? Raise the value.\n"
            "Wider than tall? Lower it.  e.g. KARAOKE_CELL_ASPECT=2.4")



def to_text(pixels: list[list[tuple[int, int, int]]], *, pad_to: int = 0):
    """Render sampled pixels as a Rich Text of background-coloured spaces.

    ``pad_to`` centres the image in a wider panel with plain spaces, so a
    portrait cover sits in the middle instead of hugging the left edge.
    """
    from rich.text import Text

    text = Text(no_wrap=True, overflow="crop")
    width = len(pixels[0]) if pixels else 0
    left = max(0, (pad_to - width) // 2)
    for y, row in enumerate(pixels):
        if left:
            text.append(" " * left)
        for (r, g, b) in row:
            text.append(" ", style=f"on rgb({r},{g},{b})")
        if y < len(pixels) - 1:
            text.append("\n")
    return text


def render(source: Path, max_cols: int, max_rows: Optional[int] = None):
    """Cover art fitted to a panel, aspect preserved, or None if unrenderable.

    The source is measured first so a 16:9 frame and a square cover both keep
    their proportions instead of being squashed into a fixed box.
    """
    if max_rows is None:
        max_rows = max(1, int(max_cols / CELL_ASPECT))
    src = probe_size(source)
    if src is None:
        return None
    size = fit(src, max_cols, max_rows)
    if size is None:
        return None
    cols, rows = size
    pixels = sample(source, cols, rows)
    return to_text(pixels, pad_to=max_cols) if pixels else None


if __name__ == "__main__":  # pragma: no cover
    print(_calibration())
