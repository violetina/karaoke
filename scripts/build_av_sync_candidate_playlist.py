#!/usr/bin/env python3
"""Build a local candidate playlist for AV-sync testing, then pre-process it.

Candidates are tracks confirmed (cheaply, via metadata only) to have two
distinct Audio/Video YouTube ids with compatible durations -- i.e. the
waveform aligner's duration guard (caption_sync._durations_compatible)
would let them through, so they're worth spending a real download +
correlation on. This does NOT touch the live active queue/playback (see
localcache.save_playlist / the saved_playlists table) -- it's a separate,
non-destructive local playlist.

Usage:
    python -m scripts.build_av_sync_candidate_playlist
"""
from __future__ import annotations

import sys
from typing import Any

from karaoke import av_align, localcache, ytmusic_client
from karaoke.caption_sync import _durations_compatible

PLAYLIST_ID = "av-sync-candidates"
PLAYLIST_NAME = "AV Sync Candidates"

# Already hand-verified this session (RMS-energy / correlation-peak evidence,
# not just the aligner's own confidence number) -- see PLAN-av-sync.md.
VERIFIED_CANDIDATES: list[tuple[str, str]] = [
    ("PJ Harvey", "Man-Size"),
    ("Queen", "Don't Stop Me Now"),
    ("Balthazar", "Bunker"),
    ("Prince", "Purple Rain"),
]


def resolve_track_id(conn: Any, artist: str, title: str) -> int | None:
    cur = conn.cursor()
    cur.execute(
        "SELECT track_id FROM tracks WHERE artist ILIKE %s AND title ILIKE %s ORDER BY track_id LIMIT 1",
        (f"%{artist}%", f"%{title}%"),
    )
    row = cur.fetchone()
    return row["track_id"] if row else None


def discover_more_candidates(conn: Any, client: ytmusic_client.YTMusicClient,
                             exclude_track_ids: set[int], limit: int,
                             pool_size: int = 400) -> list[dict[str, Any]]:
    """Sample library tracks and keep the ones with two distinct, duration-
    compatible ids -- the same cheap metadata-only check av_align itself
    uses before ever downloading anything."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT ON (t.track_id) t.track_id, t.artist, t.title
        FROM tracks t
        JOIN lyrics l ON l.track_id = t.track_id AND l.synced_lyrics IS NOT NULL AND length(l.synced_lyrics) > 0
        JOIN sources s ON s.track_id = t.track_id AND s.kind IN ('youtube', 'youtube_video', 'youtube_music')
        ORDER BY t.track_id, random()
        LIMIT %s
        """,
        (pool_size,),
    )
    pool = [dict(r) for r in cur.fetchall() if r["track_id"] not in exclude_track_ids]

    found: list[dict[str, Any]] = []
    for row in pool:
        if len(found) >= limit:
            break
        try:
            versions = client.get_audio_and_video_versions(row["artist"], row["title"])
        except Exception:
            continue
        audio_id, video_id = versions.get("audio_video_id"), versions.get("video_video_id")
        if not audio_id or not video_id or audio_id == video_id:
            continue
        if not _durations_compatible(audio_id, video_id, row["track_id"]):
            continue
        row["audio_id"], row["video_id"] = audio_id, video_id
        found.append(row)
        print(f"  found: {row['artist']} - {row['title']} (track {row['track_id']})", flush=True)
    return found


def main() -> int:
    conn = localcache.connect()
    client = ytmusic_client.YTMusicClient()

    entries: list[dict[str, Any]] = []
    for artist, title in VERIFIED_CANDIDATES:
        tid = resolve_track_id(conn, artist, title)
        if tid is None:
            print(f"  WARNING: {artist} - {title} not found in library, skipping")
            continue
        entries.append({"track_id": tid, "artist": artist, "title": title})

    print(f"Verified candidates: {len(entries)}")
    exclude = {e["track_id"] for e in entries}
    print("Scanning the library for more candidates...")
    more = discover_more_candidates(conn, client, exclude, limit=6)
    entries.extend(more)

    print(f"\nTotal candidates: {len(entries)}")
    tracks_payload = [
        {"position": i + 1, "track_id": e["track_id"], "artist": e["artist"], "title": e["title"]}
        for i, e in enumerate(entries)
    ]
    localcache.save_playlist(
        PLAYLIST_ID, PLAYLIST_NAME,
        search_query="AV sync waveform-aligner candidates",
        tracks=tracks_payload,
        source_kind="local",
        conn=conn,
    )
    print(f"Saved playlist '{PLAYLIST_NAME}' ({PLAYLIST_ID}) with {len(tracks_payload)} tracks.")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
