"""Safely batch process up to N tracks: download audio, extract chords, and index vectors.

Rate limited to avoid YouTube API throttling (default 4.0s delay between downloads).

Usage:
    python scripts/batch_process_chords_vectors.py [--limit 100] [--delay 4.0] [--skip-download]
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from karaoke import localcache, osclient
from karaoke.logger import log
from karaoke.postprocess_worker import run_download_logic, run_harmony_logic, run_vectors_logic


def find_candidate_tracks(conn: Any, limit: int = 100) -> list[dict[str, Any]]:
    query = """
        SELECT DISTINCT t.track_id, t.artist, t.title, s.url
        FROM tracks t
        JOIN sources s ON t.track_id = s.track_id
        WHERE (s.kind = 'youtube' OR s.kind = 'youtube_music')
          AND t.track_id NOT IN (
              SELECT track_id FROM opensearch_snapshots WHERE index_name = 'karaoke-clap'
          )
        ORDER BY t.track_id ASC
        LIMIT %s
    """
    rows = conn.execute(query, (limit,)).fetchall()
    return [dict(r) for r in rows]


def process_batch(candidates: list[dict[str, Any]], delay: float = 4.0, skip_download: bool = False) -> dict[str, int]:
    conn = localcache.connect()
    cli = osclient.client()

    stats = {
        "total": len(candidates),
        "downloaded": 0,
        "chords_extracted": 0,
        "vectors_indexed": 0,
        "errors": 0,
    }

    print(f"\n--- Batch processing started ({len(candidates)} tracks, delay={delay}s) ---")

    for idx, item in enumerate(candidates, 1):
        track_id = item["track_id"]
        artist = item["artist"]
        title = item["title"]
        url = item["url"]

        print(f"\n[{idx}/{len(candidates)}] Processing Track ID {track_id}: {artist} - {title}")

        audio_path: Path | None = None

        if not skip_download and url:
            try:
                # Gentle rate-limiting before fetching metadata/downloading
                if idx > 1 and delay > 0:
                    time.sleep(delay)

                audio_path = run_download_logic(url, cookies_from_browser=None)
                if audio_path and audio_path.is_file():
                    stats["downloaded"] += 1
                    print(f"  ✓ Audio downloaded/cached: {audio_path.name}")
                else:
                    print(f"  ⚠️ Audio download failed or missing for {url}")
            except Exception as exc:
                print(f"  ❌ Download error: {exc}")
                stats["errors"] += 1
                continue
        else:
            # Try to find existing cached file
            audio_path = run_download_logic(url, cookies_from_browser=None) if url else None

        # 2. Extract Key/Chords/Harmonic Progression if audio file exists
        if audio_path and audio_path.is_file():
            try:
                ok_harmony = run_harmony_logic(track_id, audio_path, conn, artist=artist, title=title)
                if ok_harmony:
                    stats["chords_extracted"] += 1
                    print(f"  ✓ Chords & harmonic motion extracted.")
                else:
                    print(f"  ⚠️ Harmony extraction returned incomplete result.")
            except Exception as exc:
                print(f"  ❌ Harmony extraction error: {exc}")

        # 3. Index Vector Embeddings (CLAP, audio, chord motion, OpenSearch)
        try:
            ok_vec = run_vectors_logic(track_id, conn)
            if ok_vec:
                stats["vectors_indexed"] += 1
                print(f"  ✓ OpenSearch vectors indexed.")
            else:
                print(f"  ⚠️ Vector indexing skipped/failed.")
        except Exception as exc:
            print(f"  ❌ Vector indexing error: {exc}")

    print("\n==================================================")
    print("Batch processing complete summary:")
    print(f"  Total Candidates:   {stats['total']}")
    print(f"  Downloaded/Cached:  {stats['downloaded']}")
    print(f"  Chords Extracted:   {stats['chords_extracted']}")
    print(f"  Vectors Indexed:    {stats['vectors_indexed']}")
    print(f"  Errors encountered: {stats['errors']}")
    print("==================================================")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch process tracks for audio, chords, and vectors.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum number of tracks to process (default: 100, max 120)")
    parser.add_argument("--delay", type=float, default=4.0, help="Polite delay in seconds between YouTube downloads (default: 4.0s)")
    parser.add_argument("--skip-download", action="store_true", help="Skip downloading audio; only process existing cached files")
    args = parser.parse_args()

    limit = min(max(1, args.limit), 120)
    conn = localcache.connect()

    candidates = find_candidate_tracks(conn, limit=limit)
    if not candidates:
        print("No un-indexed tracks found matching criteria.")
        return

    process_batch(candidates, delay=args.delay, skip_download=args.skip_download)


if __name__ == "__main__":
    main()
