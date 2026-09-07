#!/usr/bin/env python3
"""Dedicated standalone script to find tracks with missing or non-downloadable sources,
search YouTube to resolve a video, and save the source link in the local cache database.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from karaoke import localcache, youtube
from karaoke.lockfile import ProcessLock
from karaoke.logger import log


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto-fill missing or non-downloadable source links by searching YouTube."
    )
    parser.add_argument(
        "--limit", type=int, default=100,
        help="Maximum number of tracks to resolve sources for (default: 100)"
    )
    parser.add_argument(
        "--delay", type=float, default=1.0,
        help="Delay in seconds between YouTube search requests to avoid rate limits (default: 1.0)"
    )
    parser.add_argument(
        "--force-all", action="store_true",
        help="Look up sources for ALL tracks missing downloadable sources, not just those with lyrics"
    )
    args = parser.parse_args()

    print("=== KARAOKE PLATFORM SOURCE RESOLUTION & HEALING ===")
    lock = ProcessLock("source_healing")
    if not lock.acquire():
        print("Source healing/cleanup is already running in another process. Exiting.")
        sys.exit(0)

    try:
        run_fill_sources(limit=args.limit, delay=args.delay, force_all=args.force_all)
    finally:
        lock.release()


def run_fill_sources(*, limit: int, delay: float, force_all: bool) -> None:
    with localcache.connect() as conn:
        cur = conn.cursor()
        
        # Query tracks that lack a downloadable source (youtube, youtube_music, local)
        # By default, we prioritize tracks that have lyrics because those are ready to be played/sung!
        # If --force-all is set, we look up sources for every single track.
        if force_all:
            query = """
                SELECT t.track_id, t.artist, t.title FROM tracks t
                WHERE t.track_id NOT IN (
                    SELECT s.track_id FROM sources s
                    WHERE s.kind IN ('youtube', 'youtube_music', 'local')
                )
                ORDER BY t.track_id
            """
        else:
            query = """
                SELECT t.track_id, t.artist, t.title FROM tracks t
                WHERE EXISTS(SELECT 1 FROM lyrics l WHERE l.track_id = t.track_id)
                  AND t.track_id NOT IN (
                      SELECT s.track_id FROM sources s
                      WHERE s.kind IN ('youtube', 'youtube_music', 'local')
                  )
                ORDER BY t.track_id
            """

        cur.execute(query)
        tracks = cur.fetchall()

        print(f"Tracks missing downloadable sources: {len(tracks)}")
        if not tracks:
            print("No tracks need source resolution. Everything is fully populated!")
            return

        to_process = tracks[:limit]
        print(f"Resolving sources for up to {len(to_process)} tracks...")

        success_count = 0
        for i, t in enumerate(to_process, 1):
            track_id = t["track_id"]
            artist = t["artist"].strip()
            title = t["title"].strip()
            
            if not (artist or title):
                continue
                
            search_query = f"{artist} - {title}"
            print(f"[{i:3d}/{len(to_process)}] Searching YouTube for '{search_query}'...")
            
            try:
                results = youtube.search(search_query, limit=1)
                if results and results[0].get("url"):
                    best_url = results[0]["url"]
                    localcache.add_track_source(
                        artist, title, url=best_url, kind="youtube", conn=conn
                    )
                    success_count += 1
                    print(f"    ✓ Resolved -> {best_url}")
                else:
                    print("    ✗ No results found on YouTube")
            except Exception as e:
                print(f"    ✗ Error searching YouTube: {e}")
                
            if i < len(to_process):
                time.sleep(delay)

        print("\n==========================================")
        print(f"Source resolution complete! Resolved {success_count} sources successfully.")
        print("==========================================")


if __name__ == "__main__":
    main()
