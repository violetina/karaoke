#!/usr/bin/env python3
"""Batch enrich tracks with Chords, Feeling/Sentiment, and CLAP audio vectors.

Prioritizes files already on the mounted backup disk or local storage to avoid downloads.
Falls back to cached/downloaded YouTube audio when disk files are unavailable.

Pipeline steps per track:
  1. Audio resolution: Check local disk path (USB mount / home Music) -> check YT cache -> download to cache.
  2. Harmonic analysis: Key progression form + CQT chord detection + 12-bin chord motion histogram -> OpenSearch `karaoke-progression`.
  3. Audio embedding: LAION CLAP 512D text/audio shared vector -> OpenSearch `karaoke-clap`.
  4. Feeling & Text: Lyrics sentiment (dominant mood, 4D sentiment vector) + text embedding -> OpenSearch `tracks`.

Usage:
    python scripts/batch_enrich_chords_and_vectors.py --limit 50 --workers 2
    python scripts/batch_enrich_chords_and_vectors.py --limit 200 --skip-download
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

from karaoke import clap_vector, localcache, osclient, vector_index
from karaoke.key_progression import PROGRESSION_INDEX
from karaoke.logger import log
from karaoke.postprocess_worker import run_download_logic, run_harmony_logic

BACKUP_DISK_ROOT = Path("/run/media/tina/52C3-BEF5/Music-backup-DATA")


def resolve_local_audio_path(raw_url: str) -> Optional[Path]:
    """Resolve a raw source URL to an existing local file on disk."""
    if not raw_url:
        return None
    clean = urllib.parse.unquote(raw_url.replace("file://", ""))
    p = Path(clean)
    if p.is_file() and p.stat().st_size > 100 * 1024:
        return p
    # If path references Music/, test equivalent under USB backup disk
    if "/Music/" in clean:
        rel = clean.split("/Music/", 1)[-1]
        alt = BACKUP_DISK_ROOT / rel
        if alt.is_file() and alt.stat().st_size > 100 * 1024:
            return alt
    return None


def find_candidate_tracks(limit: int = 100, only_on_disk: bool = False) -> list[dict[str, Any]]:
    """Query tracks missing chord_motion in OpenSearch, prioritizing on-disk audio."""
    cli = osclient.client()
    if cli is None:
        return []

    # Get track_ids that already have chord_motion indexed (scroll past 10k limit)
    done_ids = set()
    try:
        resp = cli.search(
            index=PROGRESSION_INDEX,
            body={"query": {"exists": {"field": "chord_motion"}}, "_source": False, "size": 5000},
            scroll="2m",
        )
        scroll_id = resp.get("_scroll_id")
        hits = resp["hits"]["hits"]
        while hits:
            for hit in hits:
                try:
                    done_ids.add(int(hit["_id"].split(":")[-1]))
                except (ValueError, IndexError):
                    pass
            resp = cli.scroll(scroll_id=scroll_id, scroll="2m")
            scroll_id = resp.get("_scroll_id")
            hits = resp["hits"]["hits"]
        if scroll_id:
            try:
                cli.clear_scroll(scroll_id=scroll_id)
            except Exception:
                pass
    except Exception:
        pass

    conn = localcache.connect()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS enrichment_failures (
            track_id INTEGER PRIMARY KEY,
            reason TEXT,
            failed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    cur.execute("SELECT track_id FROM enrichment_failures")
    failed_ids = {r["track_id"] for r in cur.fetchall()}

    cur.execute("""
        SELECT t.track_id, t.artist, t.title, s.kind, s.url
        FROM tracks t
        JOIN sources s ON s.track_id = t.track_id
        WHERE length(trim(coalesce(t.artist, ''))) > 1
          AND length(trim(coalesce(t.title, ''))) > 1
          AND t.artist NOT ILIKE '%%fake%%'
          AND t.artist NOT ILIKE '%%unknown%%'
        ORDER BY t.track_id ASC
    """)
    rows = cur.fetchall()
    conn.close()

    # Group sources by track_id
    from collections import defaultdict
    tracks_sources = defaultdict(lambda: {"sources": []})
    for r in rows:
        tid = r["track_id"]
        tracks_sources[tid]["track_id"] = tid
        tracks_sources[tid]["artist"] = r["artist"].strip()
        tracks_sources[tid]["title"] = r["title"].strip()
        tracks_sources[tid]["sources"].append((r["kind"], r["url"]))

    candidates_on_disk = []
    candidates_remote = []

    for tid, data in tracks_sources.items():
        if tid in done_ids or tid in failed_ids:
            continue
        # Check if any source is on disk
        local_file = None
        for kind, url in data["sources"]:
            if kind == "local":
                p = resolve_local_audio_path(url)
                if p:
                    local_file = p
                    break
        if local_file:
            data["audio_path"] = local_file
            candidates_on_disk.append(data)
        elif not only_on_disk:
            # Check for YouTube source
            yt_url = next((u for k, u in data["sources"] if k in ("youtube", "youtube_music", "youtube_video") or "youtube.com/watch" in u or "youtu.be/" in u), None)
            if yt_url:
                data["yt_url"] = yt_url
                data["audio_path"] = None
                candidates_remote.append(data)

    combined = (candidates_on_disk + candidates_remote)[:limit]
    return combined


_thread_local = threading.local()


def _get_thread_conn():
    if not hasattr(_thread_local, "conn"):
        _thread_local.conn = localcache.connect()
    return _thread_local.conn


def process_one_track(
    item: dict[str, Any],
    skip_download: bool = False,
    sync_av: bool = False,
) -> dict[str, Any]:
    """Execute complete enrichment pipeline for a single track."""
    track_id = item["track_id"]
    artist = item["artist"]
    title = item["title"]
    audio_path: Optional[Path] = item.get("audio_path")
    yt_url: Optional[str] = item.get("yt_url")

    t0 = time.time()
    conn = _get_thread_conn()

    status = {
        "track_id": track_id,
        "artist": artist,
        "title": title,
        "harmony": False,
        "clap": False,
        "vector_doc": False,
        "error": None,
        "elapsed_s": 0.0,
    }

    # 1. Resolve Audio Path
    if not audio_path or not audio_path.is_file():
        if not yt_url and not skip_download:
            try:
                from karaoke.ytmusic_client import YTMusicClient
                yt = YTMusicClient()
                hit = yt.search_track(artist, title)
                if hit and hit.get("video_id"):
                    yt_url = f"https://www.youtube.com/watch?v={hit['video_id']}"
                    conn.execute(
                        "INSERT INTO sources (track_id, kind, url) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (track_id, "youtube", yt_url),
                    )
                    conn.commit()
            except Exception as exc:
                log.debug("Fallback YT search failed for %s: %s", track_id, exc)

        if yt_url and not skip_download:
            try:
                audio_path = run_download_logic(yt_url, cookies_from_browser=None)
            except Exception as exc:
                status["error"] = f"Download failed: {exc}"
                status["elapsed_s"] = time.time() - t0
                return status

    if not audio_path or not audio_path.is_file():
        status["error"] = "No audio file available"
        status["elapsed_s"] = time.time() - t0
        try:
            conn.execute(
                "INSERT INTO enrichment_failures (track_id, reason) VALUES (%s, %s) ON CONFLICT (track_id) DO UPDATE SET reason = EXCLUDED.reason, failed_at = CURRENT_TIMESTAMP",
                (track_id, "No audio file available"),
            )
            conn.commit()
        except Exception:
            pass
        return status

    # 2. Extract Chords & Harmonic Progression
    try:
        from karaoke import chords, key_progression
        chord_analysis = None
        try:
            chord_analysis = chords.detect(str(audio_path))
        except Exception:
            pass

        if not chord_analysis:
            status["error"] = "Audio decoding / chords.detect failed"
            status["harmony"] = False
        else:
            prog = key_progression.analyse(str(audio_path))
            if prog:
                ok_store = key_progression.store(track_id, prog, artist=artist, title=title, chords=chord_analysis)
                status["harmony"] = ok_store
            else:
                status["error"] = "Progression analysis failed"
                status["harmony"] = False
    except Exception as exc:
        status["error"] = f"Harmony error: {exc}"
        status["harmony"] = False
        log.debug("Harmony error for %s: %s", track_id, exc)

    if not status["harmony"]:
        try:
            conn.execute(
                "INSERT INTO enrichment_failures (track_id, reason) VALUES (%s, %s) ON CONFLICT (track_id) DO UPDATE SET reason = EXCLUDED.reason, failed_at = CURRENT_TIMESTAMP",
                (track_id, status.get("error") or "Harmony / chord detection failed"),
            )
            conn.commit()
        except Exception:
            pass

    # 3. Extract CLAP Audio Embedding
    try:
        vec = clap_vector.embed_audio(str(audio_path))
        if vec:
            ok_clap = clap_vector.store(track_id, vec, artist=artist, title=title)
            status["clap"] = ok_clap
    except Exception as exc:
        log.debug("CLAP error for %s: %s", track_id, exc)

    # 4. Extract Feeling & Index OpenSearch Track Vector Document
    try:
        ok_vec = vector_index.index_track(track_id, conn=conn)
        status["vector_doc"] = ok_vec
    except Exception as exc:
        log.debug("Vector index error for %s: %s", track_id, exc)

    # 5. Audio vs Video Caption Synchronization & Offset Alignment
    if sync_av:
        try:
            from karaoke.caption_sync import sync_audio_video_counterparts
            from karaoke.ytmusic_client import YTMusicClient
            av_res = sync_audio_video_counterparts(track_id, conn, client=YTMusicClient())
            if av_res.get("offset") is not None:
                status["av_sync"] = True
                status["av_offset"] = av_res["offset"]
            elif av_res.get("has_video_lyrics"):
                status["av_sync"] = True
        except Exception as exc:
            log.debug("AV sync error for %s: %s", track_id, exc)

    status["elapsed_s"] = time.time() - t0
    return status


def process_batch(
    candidates: list[dict[str, Any]],
    workers: int = 2,
    skip_download: bool = False,
    sync_av: bool = False,
) -> dict[str, int]:
    print(f"\n{'='*70}")
    print(f"  BATCH ENRICHMENT: CHORDS, FEELING & CLAP VECTORS")
    print(f"  Candidates: {len(candidates)} | Workers: {workers} | AV Sync: {sync_av}")
    print(f"{'='*70}\n")

    stats = {
        "total": len(candidates),
        "harmony_done": 0,
        "clap_done": 0,
        "vector_done": 0,
        "av_done": 0,
        "errors": 0,
    }

    print_lock = threading.Lock()
    completed = 0

    def handle_result(res: dict[str, Any]):
        nonlocal completed
        completed += 1
        tags = []
        if res["harmony"]:
            tags.append("Chords ✓")
            stats["harmony_done"] += 1
        if res["clap"]:
            tags.append("CLAP ✓")
            stats["clap_done"] += 1
        if res["vector_doc"]:
            tags.append("Feeling/Doc ✓")
            stats["vector_done"] += 1
        if res.get("av_sync"):
            off_str = f"{res.get('av_offset'):+.1f}s" if res.get('av_offset') is not None else "video"
            tags.append(f"AV({off_str}) ✓")
            stats["av_done"] += 1

        label = f"{res['artist']} - {res['title']}"
        if len(label) > 42:
            label = label[:39] + "..."

        tag_str = " ".join(tags) if tags else f"❌ {res['error'] or 'Failed'}"
        print(f"[{completed:3d}/{len(candidates)}] (ID {res['track_id']:<5}) {label:<42} → {tag_str} ({res['elapsed_s']:.1f}s)", flush=True)

        if completed % 25 == 0:
            print(f"\n--- Progress [{completed}/{len(candidates)}]: {stats['harmony_done']} Chords, {stats['clap_done']} CLAP, {stats['vector_done']} Docs, {stats['av_done']} AV Sync ---\n", flush=True)

    if workers <= 1:
        for item in candidates:
            res = process_one_track(item, skip_download=skip_download, sync_av=sync_av)
            handle_result(res)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(process_one_track, item, skip_download, sync_av) for item in candidates]
            for fut in as_completed(futures):
                res = fut.result()
                with print_lock:
                    handle_result(res)

    print(f"\n{'='*70}")
    print(f"  BATCH ENRICHMENT COMPLETE")
    print(f"{'='*70}")
    print(f"  Tracks processed     : {stats['total']}")
    print(f"  Chords & Harmony     : {stats['harmony_done']}")
    print(f"  CLAP Audio Vectors   : {stats['clap_done']}")
    print(f"  Feeling / Track Docs : {stats['vector_done']}")
    print(f"  Audio vs Video Sync  : {stats['av_done']}")
    print(f"{'='*70}\n")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=50, help="Max tracks to process (default: 50)")
    ap.add_argument("--workers", type=int, default=2, help="Number of concurrent workers (default: 2)")
    ap.add_argument("--only-on-disk", action="store_true", help="Only process tracks with audio files directly on disk")
    ap.add_argument("--skip-download", action="store_true", help="Skip downloading remote audio")
    ap.add_argument("--sync-av", action="store_true", help="Also synchronize audio vs video lyrics & offsets")
    args = ap.parse_args()

    candidates = find_candidate_tracks(limit=args.limit, only_on_disk=args.only_on_disk)
    if not candidates:
        print("No candidates needing enrichment found.")
        return 0

    print(f"Found {len(candidates)} track(s) ready for enrichment.")
    process_batch(candidates, workers=args.workers, skip_download=args.skip_download, sync_av=args.sync_av)
    return 0


if __name__ == "__main__":
    sys.exit(main())
