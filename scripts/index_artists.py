#!/usr/bin/env python3
"""Rebuild the karaoke-artists OpenSearch index from Postgres.

One document per resolved artist (scripts/resolve_artists.py), with its
Wikidata facts when scripts/enrich_artists_wikidata.py has run. The rebuild
fills a fresh index and moves the alias onto it, so searches never see a
half-built index. See docs/artist_metadata_plan.md, step 4.

Usage:
    python scripts/index_artists.py
    python scripts/index_artists.py --search "bonnie prince"
    python scripts/index_artists.py --search "" --main-genre jazz --began-decade 1950s
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from karaoke import artist_index, localcache  # noqa: E402
from karaoke.osclient import client  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--search", help="search the index instead of rebuilding it")
    ap.add_argument("--main-genre")
    ap.add_argument("--country")
    ap.add_argument("--began-decade", help="born (people) or formed (groups), e.g. 1960s")
    args = ap.parse_args(argv)

    osc = client()
    if args.search is not None:
        hits = artist_index.search(args.search, osc, main_genre=args.main_genre,
                                   country=args.country, began_decade=args.began_decade)
        for h in hits:
            print(f"{h['name'][:30]:30} {h.get('main_genre') or '':12} {h.get('country') or '':3} "
                  f"{h.get('began_decade') or '':6} {h['track_count']:>4} tracks  "
                  f"{', '.join(h.get('genres', [])[:3])[:50]}")
        return 0

    start = time.time()
    with localcache.connect() as conn:
        if not conn.execute("SELECT to_regclass('artists') AS t").fetchone()["t"]:
            print("No artists table yet: run scripts/resolve_artists.py first.")
            return 1
        result = artist_index.rebuild(conn, osc)
    print(f"{result} in {time.time() - start:.0f}s")
    return 0 if not result["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
