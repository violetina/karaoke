#!/usr/bin/env python
"""Detect and store chord progressions (v2) for library tracks with audio on disk.

Fills in `track_chords` for tracks that lack v2 chords or have not yet been analysed.
Uses multi-threaded chord detection over local audio files.

Usage:
    python scripts/backfill_chords.py --limit 50
    python scripts/backfill_chords.py --workers 4
    python scripts/backfill_chords.py --track-id 171
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from karaoke import chord_timeline, chords, key_progression, localcache  # noqa: E402


def candidates(conn, limit: Optional[int] = None) -> list[tuple[int, str, str, Path]]:
    chord_timeline.ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT t.track_id, t.artist, t.title
        FROM tracks t
        LEFT JOIN track_chords c ON c.track_id = t.track_id AND c.chords_version = %s
        WHERE c.track_id IS NULL
        ORDER BY t.play_count DESC NULLS LAST, t.track_id
        """,
        (chords.CHORDS_VERSION,),
    ).fetchall()
    out = []
    for r in rows:
        p = chord_timeline.local_audio(int(r["track_id"]), conn)
        if p is not None:
            out.append((int(r["track_id"]), r["artist"] or "", r["title"] or "", p))
            if limit and len(out) >= limit:
                break
    return out


def process_track(item: tuple[int, str, str, Path]) -> tuple[int, str, str, int, float, Optional[str]]:
    tid, artist, title, audio_path = item
    t0 = time.time()
    try:
        ans = chords.detect(str(audio_path))
        if ans is None:
            return tid, artist, title, 0, time.time() - t0, "no chords detected"
        with localcache.connect() as conn:
            row = conn.execute(
                "SELECT detected_key FROM track_analysis WHERE track_id = %s",
                (tid,),
            ).fetchone()
            key_text = (row["detected_key"] if row else "") or ""
            chord_timeline.save(tid, ans, conn, source_path=str(audio_path), key_text=key_text)
            try:
                prog = key_progression.analyse(str(audio_path))
                if prog:
                    key_progression.store(tid, prog, artist=artist, title=title, chords=ans)
            except Exception:
                pass
        return tid, artist, title, len(ans.chords), time.time() - t0, None
    except Exception as exc:
        return tid, artist, title, 0, time.time() - t0, str(exc)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--track-id", type=int, action="append", help="specific track ID(s) to backfill")
    ap.add_argument("--limit", type=int, default=0, help="maximum tracks to process (0 = all)")
    ap.add_argument("--workers", type=int, default=4, help="concurrency for chord detection (default 4)")
    args = ap.parse_args(argv)

    with localcache.connect() as conn:
        if args.track_id:
            items = []
            for tid in args.track_id:
                p = chord_timeline.local_audio(tid, conn)
                if p:
                    r = conn.execute("SELECT artist, title FROM tracks WHERE track_id = %s", (tid,)).fetchone()
                    items.append((tid, r["artist"] if r else "", r["title"] if r else "", p))
        else:
            items = candidates(conn, args.limit or None)

    total = len(items)
    print(f"Found {total} track(s) needing chord v2 with local audio.")
    if not total:
        return 0

    done = 0
    errors = 0
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_track, item): item for item in items}
        for idx, fut in enumerate(as_completed(futures), start=1):
            tid, artist, title, count, elapsed, err = fut.result()
            label = f"{artist} - {title}"[:45]
            if err:
                errors += 1
                print(f"[{idx:>3}/{total}] {tid:>5} {label:45} ERROR: {err} ({elapsed:.1f}s)")
            else:
                done += 1
                print(f"[{idx:>3}/{total}] {tid:>5} {label:45} {count:>3} chords ({elapsed:.1f}s)")

    duration = time.time() - start_time
    print(f"\nDone: {done} stored, {errors} errors in {duration:.1f}s ({duration/max(1, done):.1f}s/track)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
