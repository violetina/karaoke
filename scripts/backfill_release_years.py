"""Backfill release years: one MusicBrainz request per album, shared by its tracks.

Albums with live or compilation names are ruled out without a request; tracks
whose title carries a year (``(2004)``, ``(2021 Remaster)``) get it from there
when no album year exists. Resumable: an album already in ``album_release`` is
skipped, and an unreachable MusicBrainz stores nothing so the album is retried.

A year in an album's name ("Integrale #3 (1947)", "Beatport Top 100 June 2015") is used
as a hint for tracks that have no better source (a range uses its first year; live
albums are excluded).

``--recordings`` adds a second, per-track pass for tracks that still have no year
(compilations, no-match albums, no album): one recording search each, taking the
earliest first-release-date of the song's non-live recordings. About 1.05 s per
track, so it is opt-in; each outcome is stored and never repeated.

Usage:
    PYTHONPATH=src python scripts/backfill_release_years.py --dry-run
    PYTHONPATH=src python scripts/backfill_release_years.py --recordings --limit 50
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


def recordings_pass(conn, args) -> int:
    pending = release_year.pending_recordings(conn, args.limit or None)
    print(f"{len(pending)} tracks without a year to look up by recording "
          f"(~{len(pending) * 1.05 / 60:.0f} min)", flush=True)
    if args.dry_run:
        return 0
    print("title years:", release_year.apply_title_years(conn),
          "| album-name years:", release_year.apply_album_name_years(conn), flush=True)
    pending = release_year.pending_recordings(conn, args.limit or None)
    outcome: collections.Counter = collections.Counter()
    started = time.time()
    for i, t in enumerate(pending, 1):
        try:
            status = release_year.resolve_recording(conn, musicbrainz, t)
        except Exception as exc:  # e.g. the track was merged away while the pass ran
            conn.rollback()
            print(f"[{i}/{len(pending)}] skipped #{t['track_id']}: {exc}", flush=True)
            continue
        outcome[status] += 1
        row = conn.execute("SELECT year FROM recording_year_lookup WHERE track_id = %s",
                           (t["track_id"],)).fetchone()
        print(f"[{i}/{len(pending)}] {status:11} {row['year'] if row and row['year'] else '-':>5} "
              f"{t['artist'][:28]} | {t['title'][:44]}", flush=True)
    print(f"done in {time.time() - started:.0f}s: {dict(outcome)}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="albums to look up (0 = all pending)")
    ap.add_argument("--dry-run", action="store_true", help="count what would be looked up")
    ap.add_argument("--recordings", action="store_true",
                    help="per-track recording lookup for tracks still without a year "
                         "(instead of the album pass)")
    args = ap.parse_args(argv)

    lock = ProcessLock("release_years")
    if not args.dry_run and not lock.acquire():
        print("another release-year backfill is running", flush=True)
        return 1
    with localcache.connect() as conn:
        release_year.ensure_schema(conn)
        if args.recordings:
            return recordings_pass(conn, args)
        pending = release_year.pending_albums(conn, args.limit or None)
        by_name = collections.Counter(release_year.classify_album(p["album"]) or "lookup"
                                      for p in pending)
        tracks = sum(p["n"] for p in pending)
        print(f"{len(pending)} albums pending ({tracks} tracks): {dict(by_name)}; "
              f"~{by_name['lookup'] * 1.05 / 60:.0f} min of MusicBrainz requests", flush=True)
        if args.dry_run:
            return 0
        print("title years:", release_year.apply_title_years(conn),
              "| album-name years:", release_year.apply_album_name_years(conn), flush=True)

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
