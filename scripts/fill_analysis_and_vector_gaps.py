#!/usr/bin/env python3
"""Batch playlist runner to fill key/BPM analysis, genre classification, and vector gaps.

Identifies all tracks in the library missing Key/BPM analysis or CLAP/Genre labels,
processes them in background batches, and updates SQLite and OpenSearch vector indices.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ["TQDM_DISABLE"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

from karaoke import localcache, autoclassify, track_analysis, vector_index, analyze
from karaoke.lockfile import ProcessLock
from karaoke.logger import log


def main():
    print("=== KARAOKE PLATFORM GAP-FILL & VECTOR SAMPLER ===")
    lock = ProcessLock("gap_fill")
    if not lock.acquire():
        print("Gap-fill pipeline is already running in another process. Exiting.")
        return
    try:
        _run()
    finally:
        lock.release()


def _run():
    with localcache.connect() as conn:
        cur = conn.cursor()

        # 1. Identify tracks needing Key/BPM/Energy analysis
        cur.execute('''
            SELECT t.track_id, t.artist, t.title, s.url, s.kind
            FROM tracks t
            LEFT JOIN sources s ON s.source_id = (
                SELECT s2.source_id FROM sources s2 WHERE s2.track_id = t.track_id
                ORDER BY CASE WHEN s2.kind = 'local' THEN 0 WHEN s2.kind = 'youtube_music' THEN 1 ELSE 2 END, s2.source_id LIMIT 1
            )
            LEFT JOIN track_analysis a ON a.track_id = t.track_id
            WHERE a.bpm IS NULL OR a.detected_key IS NULL OR a.detected_key = ''
            ORDER BY t.track_id
        ''')
        missing_analysis = [dict(r) for r in cur.fetchall()]

        # 2. Identify tracks needing Genre / CLAP labels
        cur.execute('''
            SELECT t.track_id, t.artist, t.title, s.url, s.kind
            FROM tracks t
            LEFT JOIN sources s ON s.source_id = (
                SELECT s2.source_id FROM sources s2 WHERE s2.track_id = t.track_id
                ORDER BY CASE WHEN s2.kind = 'local' THEN 0 WHEN s2.kind = 'youtube_music' THEN 1 ELSE 2 END, s2.source_id LIMIT 1
            )
            LEFT JOIN track_genre g ON g.track_id = t.track_id
            WHERE g.genre IS NULL OR g.genre = ''
            ORDER BY t.track_id
        ''')
        missing_genre = [dict(r) for r in cur.fetchall()]

        print(f"Tracks missing Key/BPM/Energy:  {len(missing_analysis)}")
        print(f"Tracks missing Genre/CLAP:      {len(missing_genre)}")

        # Build Gap-Fill Playlist
        playlist_dir = Path.home() / ".local/share/karaoke"
        playlist_file = playlist_dir / "playlist_gap_fill.json"
        
        all_gap_track_ids = list(set([t["track_id"] for t in missing_analysis + missing_genre]))
        playlist_items = []
        if all_gap_track_ids:
            placeholders = ",".join("?" * len(all_gap_track_ids))
            cur.execute(f"SELECT track_id, artist, title FROM tracks WHERE track_id IN ({placeholders})", all_gap_track_ids)
            for r in cur.fetchall():
                playlist_items.append({"track_id": r["track_id"], "artist": r["artist"], "title": r["title"]})

        with playlist_file.open("w", encoding="utf-8") as f:
            json.dump({"created_at": time.time(), "total": len(playlist_items), "tracks": playlist_items}, f, indent=2)

        print(f"\nSaved gap-fill playlist ({len(playlist_items)} tracks) to {playlist_file}")

        # Step A: Run Autoclassify (Zero-shot CLAP genre classification)
        if missing_genre:
            print(f"\n--- STEP 1: Running Genre Classification on {len(missing_genre)} tracks ---")
            classified = 0
            for i, item in enumerate(missing_genre, 1):
                tid = item["track_id"]
                artist = item["artist"]
                title = item["title"]
                try:
                    res = autoclassify.run(tid, conn)
                    genre_label = res.get("genre", "") if isinstance(res, dict) else ""
                    classified += 1
                    print(f"[{i:3d}/{len(missing_genre)}] ✓ #{tid:3d}: {artist} - {title} -> {genre_label or 'classified'}")
                except Exception as exc:
                    print(f"[{i:3d}/{len(missing_genre)}] ✗ #{tid:3d}: {artist} - {title} error: {exc}")

            print(f"Genre classification complete: {classified} tracks processed.")

        # Step B: Run Local Audio Key/BPM Analysis on local files
        local_missing = [t for t in missing_analysis if t.get("kind") == "local" and t.get("url")]
        if local_missing:
            print(f"\n--- STEP 2: Analyzing {len(local_missing)} local audio files for Key/BPM ---")
            for i, item in enumerate(local_missing, 1):
                tid = item["track_id"]
                file_path = item["url"]
                artist = item["artist"]
                title = item["title"]
                try:
                    res = analyze.analyze_audio(str(file_path))
                    if res and res.key:
                        track_analysis.save_detected(tid, detected_key=res.key, bpm=res.bpm, energy=res.energy, brightness=res.brightness, method="essentia", source_kind="local", conn=conn)
                        print(f"[{i:3d}/{len(local_missing)}] ✓ #{tid:3d}: {artist} - {title} -> Key: {res.key}, BPM: {res.bpm:.1f}")
                except Exception as exc:
                    print(f"[{i:3d}/{len(local_missing)}] ✗ #{tid:3d}: {artist} - {title} error: {exc}")

        # Step C: Rebuild OpenSearch Vector Index to include all new features/genres
        print("\n--- STEP 3: Updating OpenSearch Vector Indices ---")
        try:
            st = vector_index.rebuild_from_sqlite(embed=True, include_lines=True)
            print(f"✓ OpenSearch vector index updated: tracks={st.indexed}, lines={st.line_docs}")
        except Exception as exc:
            print(f"Vector index update note: {exc}")

        print("\n==========================================")
        print("   GAP-FILL & VECTOR SAMPLING COMPLETE    ")
        print("==========================================")


if __name__ == "__main__":
    main()
