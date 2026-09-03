"""Active desktop-player detection and mode selection for the karaoke TUI.

The TUI's default is to watch whatever is playing on the desktop (via MPRIS /
playerctl) and pick a mode automatically:

- ``spotify``  — a Spotify player is active; sync/control that.
- ``scan``     — a desktop/browser player is playing (e.g. a YouTube / YT Music
                 tab in Firefox/Chrome, or VLC); sync lyrics to its position.
- ``browse``   — nothing is playing; the user drives the library list and
                 opening a song launches the browser (the old "youtube mode").

Browser MPRIS metadata is unreliable, so when a source URL is present we prefer
looking the track up by URL in the local ``sources`` table (see the
karaoke-app-development skill) before trusting artist/title.
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from typing import Optional

from .lyrics import Lyrics
from .playerctl import PlayerMetadata, current_metadata, normalize_player_track

YOUTUBE_HOSTS = ("music.youtube.com", "youtube.com/", "youtu.be/")


@dataclass(frozen=True)
class Detection:
    """The active player and the karaoke mode chosen for it."""

    mode: str  # spotify | scan | browse
    player: str = ""
    artist: str = ""
    title: str = ""
    url: str = ""
    mpris_name: str = ""

    @property
    def is_active(self) -> bool:
        """Whether a player is actually playing something to sync/control."""
        return self.mode in ("spotify", "scan")


def is_youtube_url(url: str) -> bool:
    """True if the URL points at YouTube or YouTube Music."""
    u = (url or "").lower()
    return any(host in u for host in YOUTUBE_HOSTS)


def classify(meta: Optional[PlayerMetadata]) -> Detection:
    """Map raw MPRIS metadata to a karaoke Detection/mode.

    - Spotify players -> ``spotify`` mode.
    - Any other player with a real track -> ``scan`` mode (browser YouTube tab,
      YT Music, VLC, local players).
    - Nothing playing -> ``browse`` mode (library-driven, opens the browser).
    """
    if meta is None:
        return Detection(mode="browse")
    player = (meta.mpris_name or meta.player or "").lower()
    mpris = meta.mpris_name or meta.player
    ref = normalize_player_track(meta.artist, meta.title, meta.album, meta.url)
    if player.startswith("spotify"):
        return Detection("spotify", meta.player, ref.artist, ref.title, meta.url, mpris_name=mpris)
    # A desktop or browser player is active: sync to its position. Trust a
    # YouTube URL over the (often stale) browser artist/title for display.
    if ref.title or ref.artist or meta.url:
        return Detection("scan", meta.player, ref.artist, ref.title, meta.url, mpris_name=mpris)
    return Detection(mode="browse")


def detect_active() -> Detection:
    """Detect the currently-active desktop player and its karaoke mode."""
    return classify(current_metadata())


def spotify_running() -> bool:
    """True if a Spotify MPRIS player is currently visible to playerctl."""
    try:
        out = subprocess.run(
            ["playerctl", "--list-all"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    return any(line.strip().lower().startswith("spotify") for line in out.splitlines())


def launch_spotify() -> tuple[bool, str]:
    """Deprecated: Spotify playback is unified into the browser window."""
    return False, "disabled (Spotify plays in unified browser window)"


def resolve_lyrics(
    det: Detection, conn: sqlite3.Connection
) -> tuple[str, str, Optional[Lyrics]]:
    """Resolve (artist, title, lyrics) for a detection from the local cache.

    Prefers a URL match against the ``sources`` table (canonical for browser
    tabs whose MPRIS artist/title is unreliable), then falls back to the
    normalized artist/title. Returns ``lyrics=None`` on a miss.
    """
    from . import localcache

    if det.url:
        found = localcache.find_track_by_url(det.url, conn)
        if found:
            track_id, artist, title = found
            return artist, title, localcache.get_lyrics_by_track_id(track_id, conn)
    if det.artist or det.title:
        track_id = localcache.find_track_id(det.artist, det.title, conn)
        if track_id is not None:
            return (
                det.artist,
                det.title,
                localcache.get_lyrics_by_track_id(track_id, conn),
            )
        # Browser/player titles often carry decorations the cached track lacks
        # (e.g. "(2019 Remastered)", "(Official Video)"). Retry with a cleaned
        # title before giving up so remaster/live tabs still resolve.
        from .lyrics import clean_title

        cleaned = clean_title(det.title)
        if cleaned and cleaned != det.title:
            track_id = localcache.find_track_id(det.artist, cleaned, conn)
            if track_id is not None:
                row = conn.execute(
                    "SELECT artist, title FROM tracks WHERE track_id = ?",
                    (track_id,),
                ).fetchone()
                return (
                    row["artist"] if row else det.artist,
                    row["title"] if row else cleaned,
                    localcache.get_lyrics_by_track_id(track_id, conn),
                )
        # Fallback for empty artist (e.g. browser tab playing YouTube Music without artist tag)
        if not det.artist and det.title:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT t.track_id, t.artist, t.title FROM tracks t
                WHERE lower(t.title) = lower(?)
                ORDER BY EXISTS(SELECT 1 FROM lyrics l WHERE l.track_id = t.track_id AND l.synced_lyrics != '') DESC, t.track_id DESC
                LIMIT 1
                """,
                (det.title,),
            )
            row = cur.fetchone()
            if row:
                return (
                    row["artist"],
                    row["title"],
                    localcache.get_lyrics_by_track_id(row["track_id"], conn),
                )
    return det.artist, det.title, None


def record_gap(det: Detection, conn: sqlite3.Connection) -> None:
    """Persist a missing-lyrics detection so it can be backfilled/staged.

    Stores the source (track + URL) when we have a real title, and logs a
    lyric gap so ``karaoke-backfill`` / ``karaoke-stage`` can pick it up later.
    Best-effort: never raises to the caller.
    """
    from . import localcache

    if not (det.artist or det.title):
        return
    try:
        if det.url:
            kind = "youtube" if is_youtube_url(det.url) else (det.player or "player")
            localcache.add_track_source(
                det.artist or "Unknown",
                det.title or det.url,
                url=det.url,
                kind=kind,
                player_name=det.player,
                conn=conn,
            )
        localcache.log_lyric_gap(det.artist, det.title, conn)
    except Exception:
        pass
