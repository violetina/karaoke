"""Render lyrics synced to a desktop media player's position."""
from __future__ import annotations

import time
import subprocess
from typing import Optional

from .player import LyricTimeline, get_synced
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.align import Align
from rich.text import Text

# Copied from player.py
_MOOD_BG = {
    "happy": "on green",
    "sad": "on blue",
    "angry": "on red",
    "tender": "on deep_pink4",
    "neutral": "on blue",
}
_MOOD_BORDER = {
    "happy": "green",
    "sad": "blue",
    "angry": "red",
    "tender": "magenta",
    "neutral": "cyan",
}

def _active_mood(tl: "LyricTimeline", elapsed: float) -> str:
    """Mood of the currently-active lyric line (neutral in the intro)."""
    from .sentiment import mood_of
    a = tl.active_index(elapsed)
    if a < 0:
        return "neutral"
    return mood_of(tl.lines[a][1])

def _build_frame(tl: "LyricTimeline", elapsed: float, header: str, *,
                 beat_times=None, footer_extra: str = ""):
    """Assemble the mood-tinted, beat-flashed Rich Panel for one render tick."""
    from .beats import beat_on, line_pulse

    mood = _active_mood(tl, elapsed)
    body = Text()
    
    active = tl.active_index(elapsed)
    frac = tl.active_fraction(elapsed)
    lo = max(0, active - 3)
    hi = min(len(tl.lines), active + 5)
    for i in range(lo, hi):
        line = tl.lines[i][1]
        if i == active:
            _append_lyric_line(body, line, kind="active", frac=frac, mood=mood)
        elif i < active:
            _append_lyric_line(body, line, kind="past")
        else:
            _append_lyric_line(body, line, kind="future")

    if beat_times:
        flash = beat_on(beat_times, elapsed)
    else:
        a = tl.active_index(elapsed)
        line_start = tl.lines[a][0] if a >= 0 else None
        flash = line_pulse(line_start, elapsed)

    color = _MOOD_BORDER.get(mood, "cyan")
    border_style = f"bold {color}" if flash else color

    nxt = tl.next_time(elapsed)
    foot = f"{mood}"
    foot += f"  ·  next in {nxt - elapsed:0.1f}s" if nxt else "  ·  (end)"
    if footer_extra:
        foot = f"{footer_extra}  ·  {foot}"
    return Panel(Align.left(body), title=header, subtitle=foot,
                 border_style=border_style)

def _append_lyric_line(body, line: str, *, kind: str, frac: float = 0.0,
                       mood: str = "neutral") -> None:
    """Append one lyric line to a Rich Text body."""
    if kind == "past":
        body.append("  " + line + "\n", style="dim")
        return
    if kind == "future":
        body.append("  " + line + "\n", style="grey70")
        return
    bg = _MOOD_BG.get(mood, "on blue")
    base = f"bold white {bg}"
    wi = active_word_index(line, frac)
    body.append("♪ ", style=base)
    if wi < 0:
        body.append(line + "\n", style=base)
        return
    words = line.split()
    for j, w in enumerate(words):
        if j == wi:
            body.append(w, style="bold white on magenta")
        else:
            body.append(w, style=base)
        body.append(" " if j < len(words) - 1 else "\n", style=base)

def active_word_index(text: str, frac: float) -> int:
    """Index of the word to highlight given progress `frac` (0..1) through a line."""
    words = text.split()
    if not words:
        return -1
    f = max(0.0, min(0.999999, frac))
    return min(len(words) - 1, int(f * len(words)))


def play_synced_to_player(use_cache: bool = True):
    """Render lyrics synced to a desktop media player's position."""
    console = Console()

    from .playerctl import current_songref
    ref = current_songref()
    if not ref:
        console.print("[red]No player active or metadata available via playerctl.[/red]")
        return

    console.print(f"Syncing to [bold cyan]{ref.artist} - {ref.title}[/bold cyan]...")
    
    ly = get_synced(ref.artist, ref.title, use_cache=use_cache)
    tl = LyricTimeline(ly.lines)

    if not tl.lines:
        console.print("[yellow]No synced lyrics available for this track.[/yellow]")
        return

    header = f"{ref.artist} - {ref.title}".strip(" -")

    def get_position() -> Optional[float]:
        try:
            proc = subprocess.run(
                ["playerctl", "position"],
                capture_output=True, text=True, timeout=1, check=True
            )
            return float(proc.stdout.strip())
        except (subprocess.SubprocessError, FileNotFoundError, ValueError):
            return None

    try:
        with Live(console=console, refresh_per_second=10, screen=False) as live:
            while True:
                position = get_position()
                if position is None:
                    console.print("[red]Lost connection to player.[/red]")
                    break
                    
                live.update(_build_frame(tl, position, header))
                time.sleep(0.1)
    except KeyboardInterrupt:
        pass
