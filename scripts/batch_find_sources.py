#!/usr/bin/env python3
"""Batch process local disk tracks to discover streaming sources (YouTube > Spotify),
fill metadata gaps, and perform data sanity checks with optional concurrency.

Usage:
    python scripts/batch_find_sources.py --limit 30 --dry-run
    python scripts/batch_find_sources.py --limit 1000 --workers 4 --pause 0.5
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from karaoke import localcache
from karaoke.logger import log
from karaoke.ytmusic_client import YTMusicClient
from karaoke.spotify_client import SpotifyClient, SpotifyRateLimited


def get_local_tracks_missing_sources(conn, limit: int = 1000) -> list[dict]:
    """Retrieve tracks scanned from disk that lack YouTube and Spotify sources."""
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT t.track_id, t.artist, t.title, t.duration
        FROM tracks t
        JOIN sources s ON s.track_id = t.track_id AND s.kind = 'local'
        WHERE NOT EXISTS (
            SELECT 1 FROM sources s2
            WHERE s2.track_id = t.track_id
              AND s2.kind IN ('youtube', 'youtube_music')
              AND s2.url LIKE '%%watch%%v=%%'
        )
        AND NOT EXISTS (
            SELECT 1 FROM sources s3
            WHERE s3.track_id = t.track_id
              AND s3.kind = 'spotify'
        )
        AND length(trim(coalesce(t.artist, ''))) > 1
        AND length(trim(coalesce(t.title, ''))) > 1
        AND t.artist NOT ILIKE '%%fake%%'
        AND t.artist NOT ILIKE '%%unknown%%'
        ORDER BY t.track_id
        LIMIT %s
    """, (limit,))
    return cur.fetchall()


_thread_local = threading.local()


def _get_thread_resources():
    """Lazily instantiate thread-isolated DB connection and API clients."""
    if not hasattr(_thread_local, "conn"):
        _thread_local.conn = localcache.connect()
        _thread_local.yt_client = YTMusicClient()
        _thread_local.sp_client = SpotifyClient()
    return _thread_local.conn, _thread_local.yt_client, _thread_local.sp_client


def _process_one_track(
    t: dict,
    dry_run: bool,
    both_sources: bool,
    pause_s: float,
    spotify_active: threading.Event,
) -> dict:
    conn, yt_client, sp_client = _get_thread_resources()
    tid = t["track_id"]
    artist = t["artist"].strip()
    title = t["title"].strip()
    stored_dur = t["duration"]

    found_sources = []
    best_duration: Optional[float] = None

    # 1. YouTube Discovery (Primary)
    yt_vid = None
    for _ in range(2):
        try:
            yt_vid = yt_client.search_track(artist, title)
            break
        except Exception as exc:
            err_str = str(exc).lower()
            if "429" in err_str or "rate limit" in err_str or "quota" in err_str:
                time.sleep(10.0)
            else:
                log.debug("YT search failed for %s - %s: %s", artist, title, exc)
                break

    if yt_vid:
        yt_url = f"https://www.youtube.com/watch?v={yt_vid}"
        found_sources.append(("youtube", yt_url))
        if not stored_dur:
            try:
                cp = yt_client.get_watch_counterpart(yt_vid)
                if cp and cp.get("current_length"):
                    parts = str(cp["current_length"]).split(":")
                    if len(parts) == 2:
                        best_duration = float(int(parts[0]) * 60 + int(parts[1]))
            except Exception:
                pass

    # 2. Spotify Discovery (Secondary / Complementary)
    if spotify_active.is_set() and (not yt_vid or both_sources):
        for _ in range(2):
            try:
                sp_id = sp_client.search_track(artist, title)
                if sp_id:
                    clean_id = sp_id.split(":")[-1]
                    sp_url = f"https://open.spotify.com/track/{clean_id}"
                    found_sources.append(("spotify", sp_url))
                break
            except SpotifyRateLimited as exc:
                # Rate limited on Spotify: disable across all threads so we don't stall
                log.info("Spotify rate limit reached (%s); pausing Spotify checks for this run", exc)
                spotify_active.clear()
                break
            except Exception as exc:
                err_str = str(exc).lower()
                if "429" in err_str or "rate limit" in err_str:
                    spotify_active.clear()
                    break
                log.debug("Spotify search failed for %s - %s: %s", artist, title, exc)
                break

    # 3. Store in DB
    if not dry_run and found_sources:
        cur = conn.cursor()
        for kind, url in found_sources:
            cur.execute("""
                INSERT INTO sources (track_id, kind, url)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (tid, kind, url))

        if best_duration and not stored_dur:
            cur.execute("""
                UPDATE tracks SET duration = %s WHERE track_id = %s
            """, (best_duration, tid))

        conn.commit()

    if pause_s > 0:
        time.sleep(pause_s)

    return {
        "track_id": tid,
        "artist": artist,
        "title": title,
        "sources": found_sources,
        "duration": best_duration,
    }


def process_batch(
    limit: int = 1000,
    dry_run: bool = False,
    pause_s: float = 0.5,
    both_sources: bool = True,
    workers: int = 4,
) -> dict:
    """Process a batch of local tracks to find YouTube and Spotify sources."""
    conn = localcache.connect()

    print(f"\n{'='*70}")
    print(f"  BATCH SOURCE DISCOVERY: LOCAL DISK TRACKS (YT > SPOTIFY)")
    print(f"  Concurrency: {workers} worker(s) | Pacing pause: {pause_s}s")
    print(f"{'='*70}\n")
    if dry_run:
        print("  [DRY-RUN MODE] No changes will be written to the database.\n")

    tracks = get_local_tracks_missing_sources(conn, limit=limit)
    print(f"Found {len(tracks)} local track(s) needing streaming sources.\n")
    conn.close()

    if not tracks:
        return {"processed": 0, "yt_found": 0, "sp_found": 0, "dur_updated": 0}

    total = len(tracks)
    spotify_active = threading.Event()
    spotify_active.set()

    stats = {
        "yt_found": 0,
        "sp_found": 0,
        "dur_updated": 0,
        "errors": 0,
    }

    print_lock = threading.Lock()

    if workers <= 1:
        # Single-threaded mode
        for i, t in enumerate(tracks, 1):
            res = _process_one_track(t, dry_run, both_sources, pause_s, spotify_active)
            _record_and_print(res, i, total, stats)
    else:
        # Multi-threaded concurrent worker pool
        completed_count = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(
                    _process_one_track,
                    t,
                    dry_run,
                    both_sources,
                    pause_s,
                    spotify_active,
                )
                for t in tracks
            ]
            for fut in as_completed(futures):
                res = fut.result()
                with print_lock:
                    completed_count += 1
                    _record_and_print(res, completed_count, total, stats)

    print(f"\n{'='*70}")
    print(f"  BATCH SUMMARY")
    print(f"{'='*70}")
    print(f"  Tracks processed     : {total}")
    print(f"  YouTube sources found: {stats['yt_found']}")
    print(f"  Spotify sources found: {stats['sp_found']}")
    print(f"  Duration gaps filled : {stats['dur_updated']}")
    print(f"  No source found      : {stats['errors']}")
    print(f"{'='*70}\n")

    return {
        "processed": total,
        "yt_found": stats["yt_found"],
        "sp_found": stats["sp_found"],
        "dur_updated": stats["dur_updated"],
        "errors": stats["errors"],
    }


def _record_and_print(res: dict, i: int, total: int, stats: dict):
    tid = res["track_id"]
    artist = res["artist"]
    title = res["title"]
    found_sources = res["sources"]
    best_dur = res["duration"]

    label = f"{artist} - {title}"
    if len(label) > 45:
        label = label[:42] + "..."

    tags = []
    if any(k == "youtube" for k, _ in found_sources):
        tags.append("YT ✓")
        stats["yt_found"] += 1
    if any(k == "spotify" for k, _ in found_sources):
        tags.append("SP ✓")
        stats["sp_found"] += 1
    if best_dur:
        stats["dur_updated"] += 1

    dur_tag = f" [dur: {best_dur:.0f}s]" if best_dur else ""
    if tags:
        print(f"[{i:4d}/{total}] (ID {tid:<5}) {label:<45} → {' '.join(tags)}{dur_tag}", flush=True)
    else:
        print(f"[{i:4d}/{total}] (ID {tid:<5}) {label:<45} → ✗ (no source)", flush=True)
        stats["errors"] += 1

    if i % 50 == 0:
        print(f"\n--- Checkpoint [{i}/{total}]: {stats['yt_found']} YT found, {stats['sp_found']} Spotify found, {stats['dur_updated']} durations filled ---\n", flush=True)



def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=1000, help="Max tracks to process (default 1000)")
    ap.add_argument("--workers", type=int, default=4, help="Number of concurrent workers (default 4)")
    ap.add_argument("--dry-run", action="store_true", help="Preview matches without writing to DB")
    ap.add_argument("--no-spotify", action="store_true", help="Only search YouTube, skip Spotify")
    ap.add_argument("--pause", type=float, default=0.5, help="Pause between searches per worker in seconds (default 0.5)")
    args = ap.parse_args()

    process_batch(
        limit=args.limit,
        dry_run=args.dry_run,
        pause_s=args.pause,
        both_sources=not args.no_spotify,
        workers=args.workers,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
