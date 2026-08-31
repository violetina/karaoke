"""Karaoke lyric lookup + synced terminal renderer.

Timing logic (`active_index`, `LyricTimeline`) is pure and unit-tested without
audio. Rendering uses Rich; playback clock is driven by time.monotonic() from a
user start keypress (MVP). True audio-position sync is a later upgrade.
"""
from __future__ import annotations

import time
import select
import subprocess
from dataclasses import dataclass, field
from typing import Any, Optional

from .lyrics import Lyrics, fetch_lrclib, parse_lrc

# Active-line background per detected mood (see sentiment.mood_of). Word highlight
# stays magenta on top; neutral keeps the original blue.
_MOOD_BG = {
    "happy": "on green",
    "sad": "on blue",
    "angry": "on red",
    "tender": "on deep_pink4",
    "neutral": "on blue",
}
# Panel border colour per mood — also the colour used for the beat "flash".
_MOOD_BORDER = {
    "happy": "green",
    "sad": "blue",
    "angry": "red",
    "tender": "magenta",
    "neutral": "cyan",
}


@dataclass
class LyricTimeline:
    """Ordered (time_seconds, text) lines with active-line lookup."""

    lines: list[tuple[float, str]] = field(default_factory=list)

    @property
    def times(self) -> list[float]:
        """Return only the lyric timestamps in display order."""
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

    def active_fraction(self, elapsed: float, tail: float = 4.0) -> float:
        """Progress (0..1) through the currently-active line.

        Used to interpolate a word-level highlight: LRCLIB gives only per-LINE
        timestamps, so we spread the line's on-screen duration across its words.
        Returns 0.0 in the intro (no active line). For the last line (no next
        timestamp) assumes a `tail`-second duration.
        """
        a = self.active_index(elapsed)
        if a < 0:
            return 0.0
        start = self.lines[a][0]
        end = self.lines[a + 1][0] if a + 1 < len(self.lines) else start + tail
        if end <= start:
            return 0.0
        return max(0.0, min(1.0, (elapsed - start) / (end - start)))


def timeline_from_lyrics(ly: Lyrics) -> LyricTimeline:
    """Build a renderable timeline from parsed or raw LRC lyrics."""
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
    use_cache: bool = True,
    audio_path: Optional[str] = None,
    transcribe: bool = False,
    force_transcribe: bool = False,
    stats_mode: Optional[str] = None,
) -> Lyrics:
    """Return synced lyrics, checking caches before going online."""
    from . import localcache

    def _log(event: str, ly: Optional[Lyrics] = None) -> None:
        if not stats_mode:
            return
        localcache.log_event(
            stats_mode, event, artist=artist, title=title,
            source=(ly.source if ly else ""),
            has_synced=bool(ly and ly.has_synced),
        )

    # Force path: Whisper only, no cache read, no LRCLIB.
    if force_transcribe and audio_path:
        from .whisper_sync import transcribe_to_lrc

        lrc = transcribe_to_lrc(audio_path)
        ly = Lyrics(
            plain="\n".join(t for _, t in parse_lrc(lrc)),
            synced_raw=lrc, source="whisper", lines=parse_lrc(lrc),
        ) if lrc.strip() else Lyrics()
        if ly.synced_raw or ly.plain:
            try:
                with localcache.connect() as conn:
                    localcache.add_track_and_lyrics(artist, title, ly, album=album, duration=duration, conn=conn)
            except Exception:
                pass
        return ly

    # 1. Local SQLite cache first
    if use_cache:
        with localcache.connect() as conn:
            track_id = localcache.find_track_id(artist, title, conn)
            if track_id:
                cached = localcache.get_lyrics_by_track_id(track_id, conn)
                if cached is not None and (cached.synced_raw or cached.plain):
                    _log("cache_hit", cached)
                    return cached

    if use_cache:
        _log("cache_miss")

    ly = fetch_lrclib(artist, title, album, duration)

    # Whisper fallback
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

    if not (ly.synced_raw or ly.plain):
        _log("no_lyrics", ly)

    # Write-through to local cache
    if use_cache and (ly.synced_raw or ly.plain):
        try:
            with localcache.connect() as conn:
                localcache.add_track_and_lyrics(artist, title, ly, album=album, duration=duration, conn=conn)
        except Exception:
            pass
            
    return ly


def line_nudge_delta(times: list[float], elapsed: float, direction: int) -> float:
    """Clock delta to move the highlighted line by one, in seconds."""
    if not times:
        return 0.0
    a = -1
    for i, t in enumerate(times):
        if t <= elapsed:
            a = i
        else:
            break
    if direction > 0:
        j = a + 1
        if j >= len(times):
            return 0.0
        return (times[j] + 0.05) - elapsed
    if a <= -1:
        return 0.0
    j = a - 1
    if j < 0:
        return (times[0] - 0.5) - elapsed
    return (times[j] + 0.05) - elapsed


class _KeyReader:
    # ... (rest of the file is unchanged)
    ...
