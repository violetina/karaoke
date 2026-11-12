"""Backfill release years: one MusicBrainz request per album, shared by its tracks.

Albums with live or compilation names are ruled out without a request; tracks
whose title carries a year (``(2004)``, ``(2021 Remaster)``) get it from there
when no album year exists. Resumable: an album already in ``album_release`` is
skipped, and an unreachable MusicBrainz stores nothing so the album is retried.

Usage:
    PYTHONPATH=src python scripts/backfill_release_years.py --dry-run
    PYTHONPATH=src python scripts/backfill_release_years.py --limit 20
    PYTHONPATH=src python scripts/backfill_release_years.py          # all pending

Run the full pass as a transient unit (about 1.05 s per album)::

    systemd-run --user --unit=karaoke-release-years --collect \
      --working-directory=/home/tina/karaoke --setenv=PYTHONPATH=/home/tina/karaoke/src \
      /home/tina/karaoke/.venv/bin/python -u scripts/backfill_release_years.py
"""
from __future__ import annotations

import argparse
import collections
import time

from karaoke import localcache, musicbrainz, release_year
from karaoke.lockfile import ProcessLock


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="albums to look up (0 = all pending)")
    ap.add_argument("--dry-run", action="store_true", help="count what would be looked up")
    args = ap.parse_args(argv)

    lock = ProcessLock("release_years")
    if not args.dry_run and not lock.acquire():
        print("another release-year backfill is running", flush=True)
        return 1
    with localcache.connect() as conn:
        release_year.ensure_schema(conn)
        pending = release_year.pending_albums(conn, args.limit or None)
        by_name = collections.Counter(release_year.classify_album(p["album"]) or "lookup"
                                      for p in pending)
        tracks = sum(p["n"] for p in pending)
        print(f"{len(pending)} albums pending ({tracks} tracks): {dict(by_name)}; "
              f"~{by_name['lookup'] * 1.05 / 60:.0f} min of MusicBrainz requests", flush=True)
        if args.dry_run:
            return 0
        print("title years:", release_year.apply_title_years(conn), flush=True)

        outcome: collections.Counter = collections.Counter()
        updated = 0
        started = time.time()
        for i, p in enumerate(pending, 1):
            status, n = release_year.resolve_album(conn, musicbrainz, p["artist"], p["album"],
                                                   p["arid"] or "")
            outcome[status] += 1
            updated += n
            row = conn.execute("SELECT year, mb_title FROM album_release WHERE artist_key = %s"
                               " AND album_key = %s", (release_year.norm(p["artist"]),
                                                       release_year.norm(p["album"]))).fetchone()
            shown = f"{row['year']} ({row['mb_title']})" if row and row["year"] else "-"
            print(f"[{i}/{len(pending)}] {status:11} {shown:34.34} {p['artist'][:26]} | "
                  f"{p['album'][:40]} ({p['n']} tracks, {n} set)", flush=True)
        print(f"done in {time.time() - started:.0f}s: {dict(outcome)}; {updated} track years set",
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
