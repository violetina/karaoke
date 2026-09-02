"""Upgrade cached line-level lyrics to Enhanced LRC using YouTube captions.

Tracks approved before Enhanced LRC support carry per-LINE timestamps only, so
their word highlight is interpolated. When a track has a YouTube source whose
captions are available as ``json3``, real per-word timings can be fetched and
written back with no manual tapping.

This only ever *upgrades*: a track already carrying word tags is skipped, and
the plain lyrics are preserved.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable, Optional

import requests

from . import localcache
from .caption_sync import json3_to_enhanced_lrc, probe_captions
from .lyrics import Lyrics, parse_enhanced_lrc
from .logger import log

# Bulk caption fetching trips YouTube rate limiting (HTTP 429) quickly.
DEFAULT_DELAY_S = 4.0


@dataclass
class UpgradeResult:
    """Outcome of one track upgrade attempt."""

    track_id: int
    artist: str
    title: str
    status: str          # upgraded | skipped | no-captions | error
    lines: int = 0
    words: int = 0
    detail: str = ""


def has_word_timings(synced: str) -> bool:
    """True when LRC text already carries Enhanced ``<mm:ss.xx>`` word tags."""
    if not synced:
        return False
    _, _, words = parse_enhanced_lrc(synced)
    return bool(words)


def find_upgrade_candidates(
    conn: sqlite3.Connection, limit: Optional[int] = None
) -> list[sqlite3.Row]:
    """Approved tracks with a YouTube source and line-level-only synced lyrics."""
    rows = list(conn.execute(
        """
        SELECT t.track_id, t.artist, t.title, t.album, t.duration,
               s.url, l.synced_lyrics, l.plain_lyrics
        FROM tracks t
        JOIN sources s ON s.track_id = t.track_id AND s.kind = 'youtube'
        JOIN lyrics l  ON l.track_id = t.track_id AND l.kind = 'approved'
        WHERE COALESCE(l.synced_lyrics, '') != ''
        ORDER BY t.artist, t.title
        """
    ))
    out = [r for r in rows if not has_word_timings(r["synced_lyrics"])]
    return out if limit is None else out[:limit]


def upgrade_track(
    row: sqlite3.Row,
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    cookies_from_browser: Optional[str] = None,
    cookies_file: Optional[str] = None,
) -> UpgradeResult:
    """Fetch json3 captions for one track and write back Enhanced LRC."""
    base = UpgradeResult(row["track_id"], row["artist"], row["title"], "error")
    try:
        from yt_dlp import YoutubeDL  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("caption upgrade needs yt-dlp installed") from exc

    from .youtube import _cookie_opts

    opts: dict = {
        "quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
    }
    opts.update(_cookie_opts(cookies_from_browser, cookies_file))
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(row["url"], download=False)

    avail = probe_captions(info)
    if avail.best is None or avail.best.ext != "json3":
        base.status = "no-captions"
        base.detail = avail.describe()
        return base

    response = requests.get(avail.best.url, timeout=20)
    response.raise_for_status()
    ctype = response.headers.get("Content-Type", "")
    if "html" in ctype.lower() or response.text.lstrip().startswith("<"):
        raise RuntimeError("rate limited by YouTube (HTML instead of captions)")

    enhanced = json3_to_enhanced_lrc(response.text)
    if not enhanced:
        base.status = "no-captions"
        base.detail = "caption track empty after cleanup"
        return base

    lines, _, words = parse_enhanced_lrc(enhanced)
    base.lines = len(lines)
    base.words = sum(len(w) for w in words.values())
    if not words:
        base.status = "no-captions"
        base.detail = "captions carried no word timings"
        return base

    base.status = "upgraded"
    if dry_run:
        return base

    # Preserve the existing plain lyrics: captions are for timing, and the
    # approved plain text may be a better (e.g. LRCLIB) transcription.
    plain = row["plain_lyrics"] or "\n".join(t for _, t in lines)
    localcache.put_cached_lyrics(
        row["artist"], row["title"],
        Lyrics(plain=plain, synced_raw=enhanced,
               source="youtube_caption_enhanced", lines=lines),
        album=row["album"] or "", duration=row["duration"], conn=conn,
    )
    log.info("upgraded word timings for %s - %s (%d lines)",
             row["artist"], row["title"], len(lines))
    return base


def upgrade_all(
    *,
    limit: Optional[int] = None,
    dry_run: bool = False,
    delay: float = DEFAULT_DELAY_S,
    conn: Optional[sqlite3.Connection] = None,
    cookies_from_browser: Optional[str] = None,
    cookies_file: Optional[str] = None,
    progress: bool = True,
) -> list[UpgradeResult]:
    """Upgrade every eligible cached track, stopping cleanly on rate limiting."""
    own = conn is None
    c = conn or localcache.connect()
    c.row_factory = sqlite3.Row
    results: list[UpgradeResult] = []
    try:
        candidates = find_upgrade_candidates(c, limit)
        if progress:
            print(f"{len(candidates)} track(s) with line-level timing only")
        for row in candidates:
            try:
                res = upgrade_track(
                    row, c, dry_run=dry_run,
                    cookies_from_browser=cookies_from_browser,
                    cookies_file=cookies_file,
                )
            except Exception as exc:
                res = UpgradeResult(row["track_id"], row["artist"], row["title"],
                                    "error", detail=str(exc)[:80])
            results.append(res)
            if progress:
                mark = {"upgraded": "♪", "no-captions": "-", "error": "!"}.get(res.status, " ")
                print(f"  {mark} {res.artist[:22]:22} {res.title[:28]:28} "
                      f"{res.status}"
                      + (f" ({res.lines} lines, {res.words} words)"
                         if res.status == "upgraded" else "")
                      + (f" {res.detail}" if res.detail else ""))
            # Repeated rate limiting means further calls are pointless.
            if "rate limited" in res.detail or "429" in res.detail:
                if progress:
                    print("  stopping: YouTube is rate limiting; retry later")
                break
            time.sleep(delay)
        return results
    finally:
        if own:
            c.close()


def upgrade_main(argv: Optional[list[str]] = None) -> int:
    """CLI entrypoint: ``karaoke-upgrade-timings``."""
    import argparse

    ap = argparse.ArgumentParser(
        prog="karaoke-upgrade-timings",
        description="Upgrade cached lyrics to word-level timing via YouTube captions",
    )
    ap.add_argument("--limit", type=int, default=None, help="max tracks to process")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY_S,
                    help=f"seconds between caption fetches (default {DEFAULT_DELAY_S})")
    ap.add_argument("--cookies-from-browser", metavar="BROWSER",
                    help="use logged-in browser cookies for caption access")
    ap.add_argument("--cookies", metavar="FILE", help="cookies.txt for access")
    args = ap.parse_args(argv)

    results = upgrade_all(
        limit=args.limit, dry_run=args.dry_run, delay=args.delay,
        cookies_from_browser=args.cookies_from_browser, cookies_file=args.cookies,
    )
    up = sum(1 for r in results if r.status == "upgraded")
    none = sum(1 for r in results if r.status == "no-captions")
    err = sum(1 for r in results if r.status == "error")
    verb = "would upgrade" if args.dry_run else "upgraded"
    print(f"\n{verb}={up} no-captions={none} errors={err}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(upgrade_main())
