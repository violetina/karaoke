"""Render lyrics synced to a desktop media player's position."""
from __future__ import annotations
import time
import subprocess
from typing import Optional

from rich.console import Console
from rich.live import Live

from .player import LyricTimeline, _build_frame
from .playerctl import current_metadata
from . import localcache

def play_synced_to_player() -> None:
    """Render lyrics synced to a desktop media player's position."""
    console = Console()

    # 1. Get URL from Player
    meta = current_metadata()
    if not meta or not meta.url:
        console.print("[red]No player active or metadata does not include a URL.[/red]")
        return
    
    url = meta.url
    if "youtube.com/" not in url and "youtu.be/" not in url:
        console.print(f"[yellow]Player URL is not from YouTube; cannot process.[/yellow]")
        console.print(f"[dim]{url}[/dim]")
        return

    # 2. Find Track by URL in our database
    with localcache.connect() as conn:
        track_info = localcache.find_track_by_url(url, conn)
        if not track_info:
            console.print(f"[yellow]Track URL not found in local database.[/yellow]")
            # Future improvement: We could try to resolve the URL here,
            # create a new track, and log a lyric_gap. For now, we'll just fail.
            return
        
        track_id, artist, title = track_info

        # 3. Get Lyrics
        lyrics = localcache.get_lyrics_by_track_id(track_id, conn)

    if not lyrics or not lyrics.has_synced:
        console.print(f"[yellow]No synced lyrics for '{artist} - {title}' in local database.[/yellow]")
        return

    console.print(f"Syncing to [bold cyan]{artist} - {title}[/bold cyan]...")
    tl = LyricTimeline(lyrics.lines)
    header = f"{artist} - {title}".strip(" -")

    def get_position() -> Optional[float]:
        try:
            proc = subprocess.run(
                ["playerctl", "position"],
                capture_output=True, text=True, timeout=1, check=True
            )
            return float(proc.stdout.strip())
        except (subprocess.SubprocessError, FileNotFoundError, ValueError):
            return None

    # 4. Sync Loop
    try:
        with Live(_build_frame(tl, 0, header), console=console, refresh_per_second=10, screen=False) as live:
            while True:
                position = get_position()
                if position is None:
                    console.print("[red]Lost connection to player.[/red]")
                    break
                    
                live.update(_build_frame(tl, position, header))
                time.sleep(0.1)
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped syncing.[/]")

