#!/usr/bin/env python3
"""Fetch Wikidata facts for the artists resolved by resolve_artists.py.

Genres, country, dates, record labels, members and bands, influences,
instruments, image, IDs on other services and Wikipedia articles, stored per
artist in ``artist_wikidata``. See docs/artist_metadata_plan.md, step 3.

Wikidata is fetched in batches (50 items per request), so the whole library
takes minutes. Re-running only fetches artists without a row; ``--refresh``
fetches everyone again.

Usage:
    python scripts/enrich_artists_wikidata.py --limit 20 --dry-run
    python scripts/enrich_artists_wikidata.py
    python scripts/enrich_artists_wikidata.py --refresh
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from karaoke import artist_wikidata, localcache, wikidata  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None, help="artists to process")
    ap.add_argument("--refresh", action="store_true", help="fetch artists that have a row too")
    ap.add_argument("--dry-run", action="store_true", help="fetch and print, store nothing")
    args = ap.parse_args(argv)

    start = time.time()
    with localcache.connect() as conn:
        exists = conn.execute("SELECT to_regclass('artists') AS t").fetchone()["t"]
        if not exists:
            print("No artists table yet: run scripts/resolve_artists.py first.")
            return 1
        totals = artist_wikidata.run(conn, wikidata, limit=args.limit,
                                     refresh=args.refresh, dry_run=args.dry_run)
    print(f"\n{dict(totals)} in {time.time() - start:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
