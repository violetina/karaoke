"""Flatten tracks.play_count while the library is in dev and training mode.

Play counts are not a listening signal yet: test runs logged fixture plays
into the real database (The Slits' "I Heard It Through The Grapevine" and
"Test Artist - Test Title", ~100 a day for two weeks), and radio mode logs
one airing several times within a minute. Anything ranked by play count
then leans on a handful of songs.

This recounts each track from play_events without those anomalies, then
caps the result (default 1: played or not). Counts only go down, never up.
The previous counts are saved to a backup table first.

    make flatten-counts            # cap at 1
    make flatten-counts CAP=0      # zero every count
    make flatten-counts DRY=1      # show what would change
"""
from __future__ import annotations

import argparse
import datetime as dt
from typing import Any, Iterable

from karaoke import localcache

# Plays of one song on one day above this are a test fixture or a loop, not
# listening. The fixtures logged 12-104 a day; real listening tops out at 8,
# and those 8 were one radio airing logged repeatedly.
MAX_PLAYS_PER_DAY = 10

# A play this soon after the previous counted play of the same song is the
# same play logged again. Of 676 radio repeats, 642 came within 5 minutes
# and none between 5 and 10.
SAME_PLAY_S = 600


def clean_counts(events: Iterable[dict[str, Any]]) -> dict[tuple[str, str], int]:
    """Plays per (artist, title), lowercased, after dropping anomalies.

    `events` need `artist`, `title` and `ts`, in any order.
    """
    rows = sorted(events, key=lambda r: ((r["artist"] or "").lower(), (r["title"] or "").lower(), r["ts"]))
    per_day: dict[tuple[str, str, dt.date], int] = {}
    for r in rows:
        key = ((r["artist"] or "").lower(), (r["title"] or "").lower(),
               dt.date.fromtimestamp(r["ts"]))
        per_day[key] = per_day.get(key, 0) + 1

    counts: dict[tuple[str, str], int] = {}
    last: dict[tuple[str, str], float] = {}
    for r in rows:
        song = ((r["artist"] or "").lower(), (r["title"] or "").lower())
        if per_day[(*song, dt.date.fromtimestamp(r["ts"]))] > MAX_PLAYS_PER_DAY:
            continue
        if song in last and r["ts"] - last[song] < SAME_PLAY_S:
            continue
        last[song] = r["ts"]
        counts[song] = counts.get(song, 0) + 1
    return counts


def flattened(current: int, clean: int, cap: int) -> int:
    """The new count: never above the current one, the clean recount or the cap."""
    return max(0, min(current, clean, cap))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cap", type=int, default=1, help="highest count kept (default 1)")
    parser.add_argument("--dry-run", action="store_true", help="show changes, write nothing")
    args = parser.parse_args(argv)

    backup = f"tracks_play_count_backup_{dt.datetime.now():%Y%m%d_%H%M%S}"
    with localcache.connect() as conn:
        with conn.transaction():
            events = conn.execute(
                "SELECT artist, title, ts FROM play_events WHERE event IN ('play', 'discover')"
            ).fetchall()
            clean = clean_counts(events)
            tracks = conn.execute(
                "SELECT track_id, artist, title, play_count FROM tracks WHERE play_count > 0"
            ).fetchall()
            changes = []
            for t in tracks:
                song = ((t["artist"] or "").lower(), (t["title"] or "").lower())
                new = flattened(t["play_count"], clean.get(song, 0), args.cap)
                if new != t["play_count"]:
                    changes.append((t, new))

            print(f"{len(changes)} of {len(tracks)} played tracks change, "
                  f"{sum(t['play_count'] - new for t, new in changes)} plays removed (cap {args.cap})")
            for t, new in sorted(changes, key=lambda c: c[1] - c[0]["play_count"])[:10]:
                print(f"  {t['play_count']:4d} -> {new}  {t['artist']} - {t['title']}")
            if args.dry_run or not changes:
                return 0

            conn.execute(f"CREATE TABLE {backup} AS"
                         " SELECT track_id, artist, title, play_count FROM tracks")
            for t, new in changes:
                conn.execute("UPDATE tracks SET play_count = %s WHERE track_id = %s",
                             (new, t["track_id"]))
    print(f"previous counts saved in {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
