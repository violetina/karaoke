#!/usr/bin/env python3
"""Batch synchronize audio vs video lyrics and calculate timing offsets.

For tracks that have audio synced lyrics (e.g. from LRCLIB) and a YouTube video source,
this script:
  1. Inspects the YouTube video's captions (manual or automatic).
  2. Parses and formats the captions as Video Lyrics.
  3. Aligns audio and video lyrics to derive the exact playback offset (intro/outro differences).
  4. Stores the video lyrics in the database and records the time offset in `track_sync_offsets`.
  5. Caches the completion status in `av_sync_log` to avoid redundant YouTube API queries.

Usage:
    python scripts/batch_sync_audio_video.py --limit 50 --workers 4
    python scripts/batch_sync_audio_video.py --track-id 70
    python scripts/batch_sync_audio_video.py --limit 100 --force
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

from karaoke import localcache
from karaoke.caption_sync import ensure_av_sync_log, record_av_sync_result, sync_audio_video_counterparts
from karaoke.logger import log


def ensure_log_table(conn: Any) -> None:
    """Ensure the status tracking table exists, with the method/confidence columns."""
    ensure_av_sync_log(conn)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO av_sync_log (track_id, status, offset_s)
        SELECT track_id, 'synced', offset_s FROM track_sync_offsets
        ON CONFLICT (track_id) DO NOTHING
    """)
    conn.commit()


def find_candidates(
    conn: Any,
    limit: int = 50,
    force: bool = False,
    track_id: Optional[int] = None,
    track_ids: Optional[list[int]] = None,
) -> list[dict[str, Any]]:
    """Query tracks eligible for audio vs video counterpart synchronization."""
    cur = conn.cursor()
    if track_ids:
        cur.execute(
            """
            SELECT t.track_id, t.artist, t.title, s.url
            FROM tracks t
            JOIN sources s ON s.track_id = t.track_id AND (s.kind IN ('youtube', 'youtube_video', 'youtube_music') OR s.url LIKE '%%youtube.com/watch%%')
            WHERE t.track_id = ANY(%s)
            """,
            (list(track_ids),),
        )
        return [dict(r) for r in cur.fetchall()]
    if track_id is not None:
        cur.execute(
            """
            SELECT t.track_id, t.artist, t.title, s.url
            FROM tracks t
            JOIN sources s ON s.track_id = t.track_id AND (s.kind IN ('youtube', 'youtube_video', 'youtube_music') OR s.url LIKE '%%youtube.com/watch%%')
            WHERE t.track_id = %s
            LIMIT 1
            """,
            (track_id,),
        )
        return [dict(r) for r in cur.fetchall()]

    query = """
        SELECT DISTINCT ON (t.track_id) t.track_id, t.artist, t.title, s.url
        FROM tracks t
        JOIN lyrics l ON l.track_id = t.track_id AND l.synced_lyrics IS NOT NULL AND length(l.synced_lyrics) > 0
        JOIN sources s ON s.track_id = t.track_id AND (s.kind IN ('youtube', 'youtube_video', 'youtube_music') OR s.url LIKE '%%youtube.com/watch%%')
        LEFT JOIN av_sync_log a ON a.track_id = t.track_id
        WHERE (%s OR a.track_id IS NULL OR a.method IS DISTINCT FROM 'waveform')
        ORDER BY t.track_id ASC
        LIMIT %s
    """
    cur.execute(query, (force, limit))
    return [dict(r) for r in cur.fetchall()]


_thread_local = threading.local()


def _get_thread_conn():
    if not hasattr(_thread_local, "conn"):
        _thread_local.conn = localcache.connect()
    return _thread_local.conn


def _get_thread_client():
    if not hasattr(_thread_local, "yt_client"):
        from karaoke.ytmusic_client import YTMusicClient
        _thread_local.yt_client = YTMusicClient()
    return _thread_local.yt_client


def process_one_track(
    candidate: dict[str, Any],
    cookies_from_browser: Optional[str] = None,
    enable_whisper_fallback: bool = False,
    force_waveform: bool = False,
) -> dict[str, Any]:
    track_id = candidate["track_id"]
    artist = candidate["artist"]
    title = candidate["title"]
    url = candidate.get("url")

    t0 = time.time()
    conn = _get_thread_conn()

    status = {
        "track_id": track_id,
        "artist": artist,
        "title": title,
        "url": url,
        "offset": None,
        "has_audio": False,
        "has_video": False,
        "status": "error",
        "error": None,
        "elapsed_s": 0.0,
    }

    try:
        res = sync_audio_video_counterparts(
            track_id,
            conn,
            client=_get_thread_client(),
            cookies_from_browser=cookies_from_browser,
            enable_whisper_fallback=enable_whisper_fallback,
            force_waveform=force_waveform,
        )
        status["has_audio"] = res.get("has_audio_lyrics", False)
        status["has_video"] = res.get("has_video_lyrics", False)
        status["offset"] = res.get("offset")
        status["method"] = res.get("method")
        status["confidence"] = res.get("confidence")

        status["status"] = record_av_sync_result(conn, track_id, res)
    except Exception as exc:
        status["error"] = str(exc)
        log.debug("AV sync error on track %s: %s", track_id, exc)

    status["elapsed_s"] = time.time() - t0
    return status


def run_batch_sync(
    candidates: list[dict[str, Any]],
    workers: int = 4,
    cookies_from_browser: Optional[str] = None,
    enable_whisper_fallback: bool = False,
    force_waveform: bool = False,
) -> dict[str, int]:
    print(f"\n{'='*70}")
    print(f"  AUDIO VS VIDEO COUNTERPART SYNCHRONIZATION")
    print(f"  Candidates: {len(candidates)} | Workers: {workers}"
         f"{' | Whisper fallback: ON' if enable_whisper_fallback else ''}")
    print(f"{'='*70}\n")

    stats = {
        "total": len(candidates),
        "synced": 0,
        "no_captions": 0,
        "no_audio": 0,
        "unaligned": 0,
        "errors": 0,
    }

    print_lock = threading.Lock()
    completed = 0

    def handle_result(res: dict[str, Any]):
        nonlocal completed
        completed += 1
        label = f"{res['artist']} - {res['title']}"
        if len(label) > 40:
            label = label[:37] + "..."

        if res["status"] == "synced":
            stats["synced"] += 1
            method_tag = f" [{res.get('method') or '?'}"
            method_tag += f" {res['confidence']:.2f}]" if res.get("confidence") is not None else "]"
            tag_str = f"✓ Offset: {res['offset']:+6.2f}s{method_tag}"
        elif res["status"] == "no_captions":
            stats["no_captions"] += 1
            tag_str = "⊘ No video captions"
        elif res["status"] == "no_audio_lyrics":
            stats["no_audio"] += 1
            tag_str = "⊘ No audio lyrics"
        elif res["status"] == "unaligned":
            stats["unaligned"] += 1
            tag_str = "⚠ Lyrics mismatch"
        else:
            stats["errors"] += 1
            tag_str = f"❌ Error: {res.get('error') or 'Unknown'}"

        print(
            f"[{completed:3d}/{len(candidates)}] (ID {res['track_id']:<5}) {label:<40} → {tag_str:<22} ({res['elapsed_s']:.1f}s)",
            flush=True,
        )

        if completed % 25 == 0:
            print(
                f"\n--- Progress [{completed}/{len(candidates)}]: {stats['synced']} synced, {stats['no_captions']} no captions, {stats['errors']} errors ---\n",
                flush=True,
            )

    if workers <= 1:
        for item in candidates:
            res = process_one_track(item, cookies_from_browser=cookies_from_browser,
                                    enable_whisper_fallback=enable_whisper_fallback,
                                    force_waveform=force_waveform)
            handle_result(res)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(process_one_track, item, cookies_from_browser, enable_whisper_fallback, force_waveform)
                for item in candidates
            ]
            for fut in as_completed(futures):
                res = fut.result()
                with print_lock:
                    handle_result(res)

    print(f"\n{'='*70}")
    print(f"  AUDIO VS VIDEO SYNC SUMMARY")
    print(f"{'='*70}")
    print(f"  Total processed      : {stats['total']}")
    print(f"  Successfully Synced  : {stats['synced']}")
    print(f"  No Video Captions    : {stats['no_captions']}")
    print(f"  No Audio Lyrics      : {stats['no_audio']}")
    print(f"  Unaligned / Mismatch : {stats['unaligned']}")
    print(f"  Errors               : {stats['errors']}")
    print(f"{'='*70}\n")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50, help="Max tracks to process (default: 50)")
    parser.add_argument("--workers", type=int, default=4, help="Number of worker threads (default: 4)")
    parser.add_argument("--force", action="store_true", help="Re-process already checked tracks")
    parser.add_argument("--track-id", type=int, action="append", default=None,
                        help="Process this track ID (repeatable: --track-id 1 --track-id 2)")
    parser.add_argument("--cookies-from-browser", default=None, help="Browser name for cookies (e.g. chrome)")
    parser.add_argument("--enable-whisper-fallback", action="store_true",
                        help="Opt in to the Whisper last-resort anchor for tracks the waveform and "
                             "caption methods both miss. Off by default: expensive (full-track "
                             "transcription), CPU-bound, and not fully deterministic between runs.")
    parser.add_argument("--force-waveform", action="store_true",
                        help="Re-run the waveform method even when a non-zero offset is stored "
                             "(re-verifies old caption offsets; two downloads per track).")
    args = parser.parse_args()

    conn = localcache.connect()
    ensure_log_table(conn)

    candidates = find_candidates(
        conn,
        limit=args.limit,
        force=args.force,
        track_ids=args.track_id,
    )
    conn.close()

    if not candidates:
        print("No tracks found needing audio vs video synchronization.")
        return 0

    print(f"Found {len(candidates)} candidate track(s) for audio vs video synchronization.")
    run_batch_sync(
        candidates,
        workers=args.workers,
        cookies_from_browser=args.cookies_from_browser,
        enable_whisper_fallback=args.enable_whisper_fallback,
        force_waveform=args.force_waveform,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
