#!/usr/bin/env python3
"""Resolve the library's artist strings to MusicBrainz artists and link tracks.

Each distinct artist string is matched through one of its own songs on
MusicBrainz, the artists on that recording are fetched and cached, and every
track with that string is linked to them. See docs/artist_metadata_plan.md.

MusicBrainz allows one request a second, so the whole library (~3,600 artist
strings) takes two to three hours. Re-running picks up where it stopped:
resolved and unmatched strings are skipped, failed ones retried.

After resolving, ``artist_genres`` is rewritten from the resolved artists'
MusicBrainz genre votes, which is what smart playlists, search, the graph and
the TUI's genre filter read.

Usage:
    python scripts/resolve_artists.py --limit 20 --dry-run
    python scripts/resolve_artists.py
    python scripts/resolve_artists.py --retry-unmatched
    python scripts/resolve_artists.py --genres-only
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from karaoke import artist_identity, localcache, musicbrainz  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=None, help="artist strings to process")
    ap.add_argument("--retry-unmatched", action="store_true",
                    help="also retry strings MusicBrainz had nothing convincing for")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve and print, store nothing")
    ap.add_argument("--genres-only", action="store_true",
                    help="only rewrite artist_genres from what is already resolved")
    args = ap.parse_args(argv)

    start = time.time()
    with localcache.connect() as conn:
        if not args.genres_only:
            totals = artist_identity.run(conn, musicbrainz, limit=args.limit,
                                         retry_unmatched=args.retry_unmatched,
                                         dry_run=args.dry_run)
            print(f"\n{dict(totals)} in {time.time() - start:.0f}s")
        if not args.dry_run:
            genres = artist_identity.derive_artist_genres(conn)
            print(f"artist_genres: {dict(genres)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
