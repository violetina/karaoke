from __future__ import annotations
import time
import select
import subprocess
from typing import Any, Optional

from .lyrics import Lyrics, fetch_lrclib, parse_lrc
from .player import _build_frame, get_synced, timeline_from_lyrics
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from .identify import parse_query

def play_playerctl_follow(use_cache: bool = True):  # pragma: no cover - interactive
    """Continuously follow the desktop player via playerctl."""

    console = Console()
    proc = None
    try:
        proc = subprocess.Popen(
            ["playerctl", "--follow", "metadata", "-f", "{{artist}} - {{title}}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.stdout is None:
            # This should not happen, but we need to satisfy the type checker.
            return

        console.print("[dim]Following desktop player...[/]")

        # Keep track of the last processed line to avoid re-processing on player restart
        last_line = None

        while True:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line or " - " not in line or line == last_line:
                continue
            
            last_line = line

            ref = parse_query(line)
            console.print(f"[bold cyan]Track changed:[/bold cyan] {ref.artist} - {ref.title}")
            ly = get_synced(ref.artist, ref.title, use_cache=use_cache, stats_mode="player")
            tl = timeline_from_lyrics(ly)

            if not tl.lines:
                console.print("[yellow]No synced lyrics available for this track.[/]")
                continue

            header = f"{ref.artist} - {ref.title}".strip(" -")
            
            # Since we can't get the exact position from `playerctl follow`,
            # we'll just start from the beginning of the song.
            start = time.monotonic()

            def frame() -> Panel:
                elapsed = time.monotonic() - start
                return _build_frame(tl, elapsed, header)

            with Live(frame(), console=console, refresh_per_second=10, screen=False) as live:
                while True:
                    # Check if the song has changed in the background
                    ready, _, _ = select.select([proc.stdout], [], [], 0)
                    if ready:
                        new_line = proc.stdout.readline()
                        if new_line and new_line.strip() != line:
                            break

                    elapsed = time.monotonic() - start
                    live.update(frame())
                    if tl.next_time(elapsed) is None and elapsed > tl.times[-1] + 4:
                        break
                    time.sleep(0.1)

    except (KeyboardInterrupt, SystemExit):
        console.print("\n[dim]Stopped following player.[/]")
    except FileNotFoundError:
        console.print("[red]playerctl command not found. Is it installed?[/red]")
    finally:
        if proc:
            proc.terminate()
            proc.wait()
