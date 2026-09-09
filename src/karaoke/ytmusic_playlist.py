"""Build, sync, and import YouTube Music playlists from the karaoke library in SQLite.

Provides:
- Exporting karaoke-ready tracks (tracks with approved synced lyrics) to a YouTube Music playlist.
- Exporting an active queue to a YouTube Music playlist.
- Importing an existing YouTube Music playlist into local SQLite tracks + sources.
- Listing user playlists on YouTube Music.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import localcache
from .logger import log
from .ytmusic_client import YTMusicAuthError, YTMusicClient

DEFAULT_PLAYLIST_NAME = "Karaoke (synced lyrics)"
DEFAULT_DESCRIPTION = "Tracks with time-synced lyrics in the local karaoke library."

_YT_VIDEO_ID_REGEX = re.compile(
    r"(?:v=|/v/|youtu\.be/|/embed/|/watch\?v=|/track/)([A-Za-z0-9_-]{11})"
)


def extract_video_id(url: str) -> str:
    """Extract an 11-character YouTube video ID from a URL, or return empty string."""
    if not url:
        return ""
    if len(url) == 11 and re.match(r"^[A-Za-z0-9_-]{11}$", url):
        return url
    m = _YT_VIDEO_ID_REGEX.search(url)
    return m.group(1) if m else ""


@dataclass
class Candidate:
    """One karaoke-ready track and the YouTube videoId resolved for it."""

    artist: str
    title: str
    video_id: str = ""
    resolved_by: str = ""   # stored | search | unresolved


@dataclass
class PlaylistResult:
    """Outcome of a YouTube Music playlist build or sync."""

    playlist_id: str = ""
    name: str = ""
    candidates: int = 0
    resolved: int = 0
    added: int = 0
    already_present: int = 0
    unresolved: list[Candidate] = field(default_factory=list)
    dry_run: bool = False
    completed: bool = True


def karaoke_tracks(conn: sqlite3.Connection) -> list[Candidate]:
    """Return tracks that have approved synced lyrics, with any stored YouTube video ID."""
    rows = conn.execute(
        """
        SELECT t.artist, t.title,
               (SELECT s.url FROM sources s
                 WHERE s.track_id = t.track_id AND s.kind IN ('youtube', 'youtube_music', 'http')
                   AND s.url LIKE '%youtu%'
                 ORDER BY CASE s.kind WHEN 'youtube_music' THEN 1 WHEN 'youtube' THEN 2 ELSE 3 END
                 LIMIT 1) AS yt_url
          FROM tracks t
          JOIN lyrics l ON l.track_id = t.track_id
         WHERE l.kind = 'approved'
           AND length(COALESCE(l.synced_lyrics, '')) > 0
           AND length(TRIM(COALESCE(t.artist, ''))) > 0
           AND length(TRIM(COALESCE(t.title, ''))) > 0
         GROUP BY t.track_id
         ORDER BY t.artist COLLATE NOCASE, t.title COLLATE NOCASE
        """
    ).fetchall()

    candidates: list[Candidate] = []
    for r in rows:
        vid = extract_video_id(r["yt_url"] or "")
        resolved = "stored" if vid else ""
        candidates.append(Candidate(artist=r["artist"], title=r["title"], video_id=vid, resolved_by=resolved))
    return candidates


def resolve_candidate(c: Candidate, client: YTMusicClient) -> Candidate:
    """Resolve a video ID for an unresolved candidate via YouTube Music search."""
    if c.video_id:
        return c
    vid = client.search_track(c.artist, c.title)
    if vid:
        c.video_id = vid
        c.resolved_by = "search"
    else:
        c.resolved_by = "unresolved"
    return c


def build_or_sync_ytmusic_playlist(
    *,
    candidates: list[Candidate],
    name: str = DEFAULT_PLAYLIST_NAME,
    description: str = DEFAULT_DESCRIPTION,
    dry_run: bool = False,
    client: Optional[YTMusicClient] = None,
) -> PlaylistResult:
    """Create or update a YouTube Music playlist containing the given candidate tracks."""
    yt = client or YTMusicClient()
    result = PlaylistResult(name=name, candidates=len(candidates), dry_run=dry_run)

    # 1. Resolve video IDs
    resolved_candidates: list[Candidate] = []
    for c in candidates:
        c = resolve_candidate(c, yt)
        if c.video_id:
            resolved_candidates.append(c)
            result.resolved += 1
        else:
            result.unresolved.append(c)

    if dry_run:
        return result

    yt.require_auth()

    # 2. Find or create playlist
    playlists = yt.get_library_playlists(limit=100)
    target_id: str | None = None
    for pl in playlists:
        if pl.get("title", "").strip().lower() == name.strip().lower():
            target_id = pl.get("playlistId")
            break

    video_ids = [c.video_id for c in resolved_candidates]

    if not target_id:
        target_id = yt.create_playlist(
            title=name,
            description=description,
            privacy_status="PRIVATE",
            video_ids=video_ids,
        )
        result.playlist_id = target_id
        result.added = len(video_ids)
        return result

    result.playlist_id = target_id

    # 3. Add only tracks not already in the playlist
    existing = yt.get_playlist_tracks(target_id, limit=500)
    existing_vids = {t["videoId"] for t in existing}

    needed = [vid for vid in video_ids if vid not in existing_vids]
    result.already_present = len(video_ids) - len(needed)

    if needed:
        yt.add_playlist_items(target_id, needed)
        result.added = len(needed)

    return result


def export_synced_tracks_to_ytmusic(
    *,
    name: str = DEFAULT_PLAYLIST_NAME,
    description: str = DEFAULT_DESCRIPTION,
    limit: Optional[int] = None,
    dry_run: bool = False,
    client: Optional[YTMusicClient] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> PlaylistResult:
    """Export all karaoke-ready tracks in SQLite to a YouTube Music playlist."""
    own_conn = conn is None
    c = conn or localcache.connect()
    try:
        candidates = karaoke_tracks(c)
        if limit:
            candidates = candidates[:limit]
    finally:
        if own_conn:
            c.close()

    return build_or_sync_ytmusic_playlist(
        candidates=candidates,
        name=name,
        description=description,
        dry_run=dry_run,
        client=client,
    )


def import_ytmusic_playlist_to_library(
    playlist_id: str,
    *,
    conn: Optional[sqlite3.Connection] = None,
    resolve_lyrics: bool = True,
    client: Optional[YTMusicClient] = None,
) -> dict[str, Any]:
    """Import tracks from a YouTube Music playlist into local SQLite."""
    from . import lyrics

    yt = client or YTMusicClient()
    tracks = yt.get_playlist_tracks(playlist_id, limit=500)

    own_conn = conn is None
    c = conn or localcache.connect()

    stats = {
        "playlist_id": playlist_id,
        "total_tracks": len(tracks),
        "added_tracks": 0,
        "synced_lyrics": 0,
        "plain_lyrics": 0,
        "items": [],
    }

    try:
        for t in tracks:
            artist = t["artist"]
            title = t["title"]
            album = str(t.get("album") or "")
            duration = t.get("duration_seconds")
            url = t["url"]

            # Add source and track
            track_id = localcache.add_track_source(
                artist, title, album=album, duration=duration,
                url=url, kind="youtube_music", conn=c
            )

            # Fetch lyrics if requested
            ly = None
            if resolve_lyrics:
                ly = lyrics.fetch_lrclib(artist, title, album, duration)
                if ly and ly.synced_raw:
                    stats["synced_lyrics"] += 1
                elif ly and ly.plain:
                    stats["plain_lyrics"] += 1
                localcache.add_track_and_lyrics(
                    artist, title, ly, album=album, duration=duration, conn=c
                )

            stats["added_tracks"] += 1
            stats["items"].append({
                "track_id": track_id,
                "artist": artist,
                "title": title,
                "url": url,
                "has_synced": bool(ly and ly.synced_raw),
            })
    finally:
        if own_conn:
            c.close()

    return stats


def interactive_setup_auth() -> None:
    """Interactively setup authentication headers using ytmusicapi.setup()."""
    from ytmusicapi import setup
    dest = Path("~/.config/karaoke/ytmusic_auth.json").expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    print("=" * 60)
    print("YouTube Music Browser Headers Setup")
    print("=" * 60)
    print(f"Target destination: {dest}\n")
    print("HOW TO GET HEADERS FROM CHROME (in 4 quick steps):")
    print(" 1. In Chrome, open https://music.youtube.com (logged in).")
    print(" 2. Press F12 (or Ctrl+Shift+I) to open Developer Tools -> click 'Network' (or 'Netwerk').")
    print(" 3. Filter by 'browse' or click on any playlist / song on YouTube Music.")
    print(" 4. In the network list, right click 'browse' -> Copy -> 'Copy request headers'")
    print("    (In Dutch: Rechtermuisklik -> Kopiëren -> Aanvraagheaders kopiëren)")
    print("    (Or: Copy -> 'Copy as cURL (POSIX)')\n")
    print("Now paste your copied headers below and press Enter (or Ctrl+D / Ctrl+Z on an empty line when finished):\n")
    try:
        setup(filepath=str(dest))
        print("\n" + "=" * 60)
        print(f"SUCCESS: Authentication saved to {dest}")
        print("You can now run: karaoke-ytmusic-playlist --list")
        print("=" * 60)
    except Exception as exc:
        print(f"\nSetup failed or cancelled: {exc}")


def ytmusic_playlist_main(argv: list[str] | None = None) -> None:
    """CLI entrypoint for managing YouTube Music playlists."""
    parser = argparse.ArgumentParser(
        description="Sync, build, or import YouTube Music playlists from the karaoke library."
    )
    parser.add_argument("--list", action="store_true", help="List YouTube Music playlists in your library")
    parser.add_argument("--sync", action="store_true", help="Sync karaoke-ready tracks to YouTube Music playlist")
    parser.add_argument("--import-playlist", metavar="PLAYLIST_ID", help="Import a YouTube Music playlist into local library")
    parser.add_argument("--name", default=DEFAULT_PLAYLIST_NAME, help=f"Playlist name (default: '{DEFAULT_PLAYLIST_NAME}')")
    parser.add_argument("--limit", type=int, help="Limit number of tracks to export")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without modifying YouTube Music")
    parser.add_argument("--setup-auth", action="store_true", help="Interactively set up YouTube Music authentication headers")

    args = parser.parse_args(argv)

    if args.setup_auth:
        interactive_setup_auth()
        return

    client = YTMusicClient()

    if args.list:
        try:
            playlists = client.get_library_playlists(limit=50)
            if not playlists:
                print("No playlists found in your YouTube Music library.")
                return
            print(f"Found {len(playlists)} playlist(s):")
            for p in playlists:
                title = p.get("title", "Untitled")
                pid = p.get("playlistId", "")
                count = p.get("count", "?")
                print(f" - {title} ({count} tracks) [ID: {pid}]")
        except YTMusicAuthError as exc:
            print(f"Authentication required: {exc}")
            sys.exit(1)
        return

    if args.import_playlist:
        print(f"Importing YouTube Music playlist {args.import_playlist}...")
        stats = import_ytmusic_playlist_to_library(args.import_playlist, client=client)
        print(f"Imported {stats['added_tracks']}/{stats['total_tracks']} tracks:")
        print(f" - Synced lyrics matched: {stats['synced_lyrics']}")
        print(f" - Plain lyrics matched:  {stats['plain_lyrics']}")
        return

    if args.sync or not (args.list or args.import_playlist):
        print(f"Building/syncing playlist '{args.name}'...")
        try:
            res = export_synced_tracks_to_ytmusic(
                name=args.name,
                limit=args.limit,
                dry_run=args.dry_run,
                client=client,
            )
            print(f"Playlist: {res.name} (ID: {res.playlist_id or 'dry-run'})")
            print(f" - Candidates: {res.candidates}")
            print(f" - Resolved:   {res.resolved}")
            print(f" - Added:      {res.added}")
            print(f" - Present:    {res.already_present}")
            print(f" - Unresolved: {len(res.unresolved)}")
            if res.dry_run:
                print("[DRY RUN] No changes were made to YouTube Music.")
        except YTMusicAuthError as exc:
            print(f"Authentication required: {exc}")
            sys.exit(1)


if __name__ == "__main__":
    ytmusic_playlist_main()
