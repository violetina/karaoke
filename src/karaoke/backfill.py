"""Automated lyric backfill system."""
from __future__ import annotations
import argparse
from typing import Optional

from . import localcache

def backfill_main(argv: Optional[list[str]] = None) -> int:
    """Run the `karaoke-backfill` CLI to fill lyric gaps."""
    ap = argparse.ArgumentParser(
        prog="karaoke-backfill",
        description="Automated lyric backfill system",
    )
    ap.add_argument("--list", action="store_true", help="list pending gaps")
    ap.add_argument("--run", action="store_true", help="run the backfill process")
    args = ap.parse_args(argv)

    if args.list:
        with localcache.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT artist, title FROM lyric_gaps WHERE status = 'pending'")
            for row in cur:
                print(f"{row['artist']} - {row['title']}")
        return 0
    
    if args.run:
        from . import backfill_runner
        backfill_runner.run()
        return 0

    return 0
