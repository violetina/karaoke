#!/usr/bin/env python
"""Compute mood timelines for tracks whose audio is on disk.

New tracks get one from the post-processing chain (the CLAP worker). This
fills in the library that predates it. Loads CLAP in this process (~1.3 GB);
roughly 4-15 s per track on CPU, most of it CLAP over ten-second windows.

Usage::

    python scripts/backfill_moods.py --limit 50
    python scripts/backfill_moods.py --track-id 1606 --track-id 146
    python scripts/backfill_moods.py --recalibrate --limit 0
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from karaoke import chord_timeline, localcache, mood_timeline  # noqa: E402


def candidates(conn, limit: Optional[int]) -> list[int]:
    mood_timeline.ensure_schema(conn)
    rows = conn.execute(
        "SELECT t.track_id FROM tracks t"
        " LEFT JOIN track_moods m ON m.track_id = t.track_id AND m.mood_version = %s"
        " WHERE m.track_id IS NULL ORDER BY t.play_count DESC NULLS LAST, t.track_id",
        (mood_timeline.MOOD_VERSION,)).fetchall()
    out = []
    for row in rows:
        if chord_timeline.local_audio(int(row["track_id"]), conn) is not None:
            out.append(int(row["track_id"]))
            if limit and len(out) >= limit:
                break
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--track-id", type=int, action="append")
    ap.add_argument("--limit", type=int, default=50, help="0 = no limit")
    ap.add_argument("--recalibrate", action="store_true",
                    help="re-measure the prompt baselines over the library first")
    args = ap.parse_args(argv)
    if args.recalibrate:
        cal = mood_timeline.build_calibration()
        print(f"calibrated on {cal['samples']}")
    with localcache.connect() as conn:
        ids = args.track_id or candidates(conn, args.limit or None)
        print(f"{len(ids)} track(s)")
        done = failed = 0
        for n, tid in enumerate(ids, start=1):
            t0 = time.time()
            tl = mood_timeline.analyse_track(tid, conn)
            name = conn.execute("SELECT artist || ' - ' || title AS n FROM tracks"
                                " WHERE track_id = %s", (tid,)).fetchone()["n"]
            if tl is None:
                failed += 1
                print(f"[{n}/{len(ids)}] {tid:>6} {name[:40]:40} no audio")
                continue
            done += 1
            s = tl.summary
            print(f"[{n}/{len(ids)}] {tid:>6} {name[:40]:40} {s['dominant']:6} "
                  f"swings={s['swing_count']} border={s['borderline_share']:.0%} "
                  f"({time.time() - t0:.1f}s)  {s['arc'][:60]}")
    print(f"\n{done} stored, {failed} without audio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
