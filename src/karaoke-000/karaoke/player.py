"""Karaoke lyric lookup + synced terminal renderer.

Timing logic (`active_index`, `LyricTimeline`) is pure and unit-tested without
audio. Rendering uses Rich; playback clock is driven by time.monotonic() from a
user start keypress (MVP). True audio-position sync is a later upgrade.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .lyrics import Lyrics, fetch_lrclib, parse_lrc


@dataclass
class LyricTimeline:
    """Ordered (time_seconds, text) lines with active-line lookup."""

    lines: list[tuple[float, str]] = field(default_factory=list)

    @property
    def times(self) -> list[float]:
        return [t for t, _ in self.lines]

    def active_index(self, elapsed: float) -> int:
        """Index of the line active at `elapsed` seconds.

        Returns -1 before the first line's timestamp (intro), otherwise the
        index of the latest line whose timestamp is <= elapsed.
        """
        idx = -1
        for i, (t, _) in enumerate(self.lines):
            if t <= elapsed:
                idx = i
            else:
                break
        return idx

    def next_time(self, elapsed: float) -> Optional[float]:
        """Timestamp of the next upcoming line, or None if past the end."""
        for t, _ in self.lines:
            if t > elapsed:
                return t
        return None


def timeline_from_lyrics(ly: Lyrics) -> LyricTimeline:
    if ly.lines:
        return LyricTimeline(list(ly.lines))
    if ly.synced_raw:
        return LyricTimeline(parse_lrc(ly.synced_raw))
    return LyricTimeline([])


def get_synced(
    artist: str,
    title: str,
    album: str = "",
    duration: Optional[float] = None,
    *,
    os_client: Any = None,
    use_cache: bool = True,
    audio_path: Optional[str] = None,
    transcribe: bool = False,
) -> Lyrics:
    """Return synced lyrics: OpenSearch cache first, then LRCLIB (and cache it).

    If LRCLIB has no synced lyrics and `transcribe=True` with a local
    `audio_path`, fall back to Whisper transcription and cache the result.
    """
    from .config import settings

    c = None
    if use_cache:
        try:
            from .osclient import client
            from .search import find_track

            c = os_client or client()
            src = find_track(artist, title, os_client=c)
            if src and src.get("synced_lyrics"):
                return Lyrics(
                    plain=src.get("plain_lyrics", ""),
                    synced_raw=src["synced_lyrics"],
                    source=src.get("lyrics_source", "lrclib"),
                    lines=parse_lrc(src["synced_lyrics"]),
                )
        except Exception:
            c = None  # cache unavailable -> fall through to live fetch

    ly = fetch_lrclib(artist, title, album, duration)

    # Whisper fallback: no synced lyrics from LRCLIB but we have local audio.
    if not ly.has_synced and transcribe and audio_path:
        try:
            from .whisper_sync import transcribe_to_lrc

            lrc = transcribe_to_lrc(audio_path)
            if lrc.strip():
                ly = Lyrics(
                    plain="\n".join(t for _, t in parse_lrc(lrc)),
                    synced_raw=lrc,
                    source="whisper",
                    lines=parse_lrc(lrc),
                )
        except Exception:
            pass

    # Best-effort write-through cache so the next play is offline.
    if use_cache and c is not None and (ly.synced_raw or ly.plain):
        try:
            import hashlib
            from datetime import datetime, timezone

            _id = "lrc:" + hashlib.sha1(f"{artist}\n{title}".encode()).hexdigest()
            c.index(index=settings.index_name, id=_id, body={
                "title": title, "artist": artist, "album": album,
                "duration": duration, "source": "lrclib-cache",
                "has_synced": ly.has_synced, "lyrics_source": ly.source,
                "plain_lyrics": ly.plain, "synced_lyrics": ly.synced_raw,
                "indexed_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            pass
    return ly


def render_lines(tl: LyricTimeline, active: int, context: int = 3) -> str:
    """Plain-text window around the active line (used by tests + fallback)."""
    if not tl.lines:
        return "(no synced lyrics)"
    lo = max(0, active - context)
    hi = min(len(tl.lines), active + context + 1)
    out = []
    for i in range(lo, hi):
        prefix = ">> " if i == active else "   "
        out.append(f"{prefix}{tl.lines[i][1]}")
    return "\n".join(out)


def play(tl: LyricTimeline, *, title: str = "", artist: str = "",
         offset: float = 0.0) -> None:  # pragma: no cover - interactive
    """Render synced lyrics with a Rich Live view, clock from keypress."""
    from rich.align import Align
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text

    console = Console()
    if not tl.lines:
        console.print("[yellow]No synced lyrics available for this track.[/]")
        return

    header = f"{artist} - {title}".strip(" -")
    console.print(f"[bold cyan]{header}[/]")
    console.input("[dim]Press Enter when the music starts...[/] ")
    start = time.monotonic()

    def frame() -> Panel:
        elapsed = time.monotonic() - start + offset
        active = tl.active_index(elapsed)
        body = Text()
        lo = max(0, active - 3)
        hi = min(len(tl.lines), active + 5)
        for i in range(lo, hi):
            line = tl.lines[i][1]
            if i == active:
                body.append("♪ " + line + "\n", style="bold white on blue")
            elif i < active:
                body.append("  " + line + "\n", style="dim")
            else:
                body.append("  " + line + "\n", style="grey70")
        nxt = tl.next_time(elapsed)
        footer = f"  next in {nxt - elapsed:0.1f}s" if nxt else "  (end)"
        return Panel(Align.left(body), title=header, subtitle=footer.strip())

    try:
        with Live(frame(), console=console, refresh_per_second=10, screen=False) as live:
            while True:
                elapsed = time.monotonic() - start + offset
                live.update(frame())
                if tl.next_time(elapsed) is None and \
                        elapsed > tl.times[-1] + 4:
                    break
                time.sleep(0.1)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/]")
