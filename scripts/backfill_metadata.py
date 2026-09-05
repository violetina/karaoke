"""Fill in missing track duration and album from what is already available.

``detect`` now captures both from MPRIS, so they fill in as tracks play — but
the library already holds hundreds of rows that predate that, and duration in
particular is what lets the deduplicator tell a demo from the studio cut.

Sources, cheapest first, because each is strictly more expensive than the last:

1. **Cached audio** — ffprobe on files already downloaded. Free, offline, and
   exact: it is the actual recording being measured.
2. **Spotify** — one lookup per track that has a Spotify source. Gives album
   *and* duration. Uses ``/v1/tracks/{id}``, a plain lookup, not the
   rate-limited search endpoint this project has exhausted before.
3. **yt-dlp** — a metadata fetch per YouTube URL. Slowest by far and opt-in
   (``--slow``), for tracks the first two cannot reach.

Nothing here overwrites a value that is already stored: absence is filled,
disagreement is left alone. A source that does not know stays silent rather
than writing a zero, which would read as a real, absurdly short track.

Run:  ``python scripts/backfill_metadata.py --dry-run``
      ``python scripts/backfill_metadata.py --slow --reindex``
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from karaoke import localcache                       # noqa: E402
from karaoke.config import settings                  # noqa: E402

_SPOTIFY_ID = re.compile(r"(?:track[/:])([A-Za-z0-9]+)")


def probe_duration(path: Path) -> Optional[float]:
    """Exact length of a local file, via ffprobe."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30, check=True).stdout.strip()
        value = float(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return value if value > 0 else None


def spotify_id(url: str) -> Optional[str]:
    match = _SPOTIFY_ID.search(url or "")
    return match.group(1) if match else None


def rows_missing(conn) -> list:
    """Tracks missing a duration or an album, with their source URLs."""
    return conn.execute(
        """
        SELECT t.track_id, t.artist, t.title, t.album, t.duration,
               (SELECT group_concat(s.url, char(10)) FROM sources s
                 WHERE s.track_id = t.track_id) AS urls
        FROM tracks t
        WHERE t.duration IS NULL OR t.duration <= 0
           OR COALESCE(t.album, '') = ''
        ORDER BY t.track_id
        """
    ).fetchall()


def store(conn, track_id: int, *, duration: Optional[float] = None,
          album: str = "") -> None:
    """Write what was learned, never clobbering what is already known."""
    conn.execute(
        """
        UPDATE tracks
        SET duration = COALESCE(duration, ?),
            album = COALESCE(NULLIF(album, ''), NULLIF(?, ''))
        WHERE track_id = ?
        """,
        (duration, album, track_id),
    )


def from_cache(conn, rows, *, apply: bool) -> int:
    """Step 1: measure cached audio. Free and exact."""
    cache = {p.stem: p for p in Path(settings.youtube_dir).glob("*") if p.is_file()}
    filled = 0
    for row in rows:
        if row["duration"]:
            continue
        for url in (row["urls"] or "").splitlines():
            vid = localcache.extract_youtube_id(url)
            path = cache.get(vid or "")
            if not path:
                continue
            seconds = probe_duration(path)
            if seconds is None:
                continue
            print(f"  cache   {row['artist'][:22]:<24} {row['title'][:26]:<28} "
                  f"{seconds:6.1f}s")
            if apply:
                store(conn, row["track_id"], duration=seconds)
            filled += 1
            break
    return filled


def from_spotify(conn, rows, *, apply: bool) -> int:
    """Step 2: one lookup per track with a Spotify source. Album and duration."""
    from karaoke.spotify_client import (SpotifyAuthError, SpotifyClient,
                                        SpotifyRateLimited)

    wanted: dict[str, list] = {}
    for row in rows:
        for url in (row["urls"] or "").splitlines():
            sid = spotify_id(url) if "spotify" in url else None
            if sid:
                wanted.setdefault(sid, []).append(row)
                break
    if not wanted:
        return 0

    print(f"  looking up {len(wanted)} track(s) on Spotify…")
    try:
        found = SpotifyClient().tracks_by_id(list(wanted))
    except SpotifyRateLimited as exc:
        print(f"  ! rate limited; retry in {exc.retry_after}s. Skipping Spotify.")
        return 0
    except SpotifyAuthError as exc:
        print(f"  ! Spotify unavailable: {exc}")
        return 0

    filled = 0
    for sid, item in found.items():
        album = (item.get("album") or {}).get("name") or ""
        seconds = (item.get("duration_ms") or 0) / 1000.0 or None
        for row in wanted.get(sid, []):
            print(f"  spotify {row['artist'][:22]:<24} {row['title'][:26]:<28} "
                  f"{seconds or 0:6.1f}s  {album[:24]}")
            if apply:
                store(conn, row["track_id"], duration=seconds, album=album)
            filled += 1
    return filled


def from_ytdlp(conn, rows, *, apply: bool) -> int:
    """Step 3: a metadata fetch per YouTube URL. Slow, so opt-in."""
    filled = 0
    for row in rows:
        if row["duration"] and (row["album"] or ""):
            continue
        url = next((u for u in (row["urls"] or "").splitlines()
                    if localcache.extract_youtube_id(u)), "")
        if not url:
            continue
        try:
            out = subprocess.run(
                ["yt-dlp", "--skip-download", "--print", "%(duration)s|%(album)s",
                 url],
                capture_output=True, text=True, timeout=60, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        raw_duration, _, raw_album = out.partition("|")
        try:
            seconds = float(raw_duration)
        except ValueError:
            seconds = 0.0
        album = "" if raw_album in ("NA", "None") else raw_album.strip()
        if not seconds and not album:
            continue
        print(f"  yt-dlp  {row['artist'][:22]:<24} {row['title'][:26]:<28} "
              f"{seconds:6.1f}s  {album[:24]}")
        if apply:
            store(conn, row["track_id"], duration=seconds or None, album=album)
        filled += 1
    return filled


def reindex() -> None:
    """Refresh the OpenSearch documents so they carry the new fields.

    The index is derived from SQLite, which is why it held empty albums and no
    durations: it can only ever mirror what was there when it was built.
    """
    from karaoke.vector_index import vector_index_main

    print("\n=== Reindexing OpenSearch ===")
    try:
        vector_index_main([])
    except Exception as exc:
        print(f"  ! reindex skipped: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the results (default is a dry run)")
    ap.add_argument("--slow", action="store_true",
                    help="also query yt-dlp per track for what is left")
    ap.add_argument("--reindex", action="store_true",
                    help="refresh the OpenSearch documents afterwards")
    args = ap.parse_args()
    apply = args.apply

    with localcache.connect() as conn:
        rows = rows_missing(conn)
        missing_duration = sum(1 for r in rows if not r["duration"])
        missing_album = sum(1 for r in rows if not (r["album"] or ""))
        print(f"{len(rows)} track(s) incomplete: "
              f"{missing_duration} without duration, {missing_album} without album\n")

        print("=== Step 1: cached audio (free) ===")
        one = from_cache(conn, rows, apply=apply)
        print(f"  filled {one}\n")

        print("=== Step 2: Spotify lookup ===")
        two = from_spotify(conn, rows, apply=apply)
        print(f"  filled {two}\n")

        three = 0
        if args.slow:
            print("=== Step 3: yt-dlp (slow) ===")
            rows = rows_missing(conn) if apply else rows
            three = from_ytdlp(conn, rows, apply=apply)
            print(f"  filled {three}\n")
        else:
            print("=== Step 3: yt-dlp skipped (pass --slow) ===\n")

        if apply:
            conn.commit()
        print(f"Total: {one + two + three} filled"
              f"{'' if apply else '  (dry run — nothing written)'}")

    if args.reindex and apply:
        reindex()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
