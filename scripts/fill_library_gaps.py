#!/usr/bin/env python
"""Download audio and backfill library gaps for chords v2 and mood v2.

Queries library tracks that lack chords v2 (track_chords) or mood timeline v2
(track_moods), resolves or downloads their audio into the local cache,
and computes both v2 representations.

Usage:
    python scripts/fill_library_gaps.py --limit 20
    python scripts/fill_library_gaps.py --limit 0
    python scripts/fill_library_gaps.py --max-per-artist 0
    python scripts/fill_library_gaps.py --exclude-artist sinatra --exclude-artist "ella fitzgerald"
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from karaoke import (  # noqa: E402
    chord_timeline,
    chords,
    key_progression,
    localcache,
    mood_timeline,
)
from karaoke.logger import log  # noqa: E402
from karaoke.postprocess_worker import run_download_logic  # noqa: E402


def fetch_candidates(
    conn,
    limit: Optional[int] = None,
    exclude_artists: Optional[list[str]] = None,
    max_per_artist: int = 0,
) -> list[dict]:
    chord_timeline.ensure_schema(conn)
    mood_timeline.ensure_schema(conn)
    query = """
        SELECT
            t.track_id,
            t.artist,
            t.title,
            t.play_count,
            a.detected_key,
            c.chords_version,
            m.mood_version,
            s.sources
        FROM tracks t
        LEFT JOIN track_analysis a ON a.track_id = t.track_id
        LEFT JOIN track_chords c ON c.track_id = t.track_id AND c.chords_version = %s
        LEFT JOIN track_moods m ON m.track_id = t.track_id AND m.mood_version = %s
        JOIN LATERAL (
            SELECT json_agg(
                json_build_object('url', url, 'kind', kind)
                ORDER BY CASE WHEN kind = 'local' THEN 0 WHEN kind = 'youtube_music' THEN 1 ELSE 2 END
            ) AS sources
            FROM sources
            WHERE track_id = t.track_id
              AND kind IN ('youtube', 'youtube_music', 'local')
        ) s ON s.sources IS NOT NULL
        WHERE (c.track_id IS NULL OR m.track_id IS NULL)
          AND length(trim(coalesce(t.artist, ''))) > 1
          AND length(trim(coalesce(t.title, ''))) > 1
          AND NOT (t.artist ILIKE ANY(%s))
        ORDER BY t.play_count DESC NULLS LAST, t.track_id ASC
    """
    exclude_patterns = [f"%{a.strip()}%" for a in exclude_artists or [] if a.strip()]
    rows = conn.execute(
        query, (chords.CHORDS_VERSION, mood_timeline.MOOD_VERSION, exclude_patterns)
    ).fetchall()
    candidates = [dict(r) for r in rows]
    if max_per_artist > 0:
        per_artist: dict[str, int] = {}
        capped = []
        for cand in candidates:
            artist_key = (cand["artist"] or "").strip().lower()
            if per_artist.get(artist_key, 0) < max_per_artist:
                per_artist[artist_key] = per_artist.get(artist_key, 0) + 1
                capped.append(cand)
        candidates = capped
    if limit and limit > 0:
        candidates = candidates[:limit]
    return candidates


def process_candidate(
    cand: dict,
    conn,
    *,
    skip_chords: bool = False,
    skip_moods: bool = False,
) -> dict:
    tid = cand["track_id"]
    artist = cand["artist"] or ""
    title = cand["title"] or ""
    sources = cand.get("sources") or []
    key_text = cand["detected_key"] or ""
    needs_chords = not skip_chords and cand.get("chords_version") != chords.CHORDS_VERSION
    needs_mood = not skip_moods and cand.get("mood_version") != mood_timeline.MOOD_VERSION

    result = {
        "track_id": tid,
        "artist": artist,
        "title": title,
        "chords": False,
        "mood": False,
        "downloaded": False,
        "error": None,
        "elapsed_s": 0.0,
    }
    t0 = time.time()

    # 1. Resolve or download audio
    audio_path = chord_timeline.local_audio(tid, conn)
    if audio_path is None:
        # Try downloadable URLs from candidates (e.g. youtube, youtube_music)
        download_urls = [
            s["url"] for s in sources
            if s.get("kind") in ("youtube", "youtube_music") and s.get("url")
        ]
        last_dl_err = None
        for dl_url in download_urls:
            try:
                audio_path = run_download_logic(dl_url, cookies_from_browser=None)
                if audio_path and audio_path.is_file():
                    result["downloaded"] = True
                    break
            except Exception as exc:
                last_dl_err = exc
                continue

        if not audio_path and last_dl_err:
            result["error"] = f"Download failed: {last_dl_err}"
            result["elapsed_s"] = time.time() - t0
            return result

    if not audio_path or not audio_path.is_file():
        result["error"] = "No audio available"
        result["elapsed_s"] = time.time() - t0
        return result

    # 2. Chords v2
    if needs_chords:
        try:
            ans = chords.detect(str(audio_path))
            if ans is not None:
                chord_timeline.save(
                    tid, ans, conn, source_path=str(audio_path), key_text=key_text
                )
                prog = key_progression.analyse(str(audio_path))
                if prog is not None:
                    key_progression.store(
                        tid, prog, artist=artist, title=title, chords=ans
                    )
                result["chords"] = True
        except Exception as exc:
            log.warning("Chords v2 failed for %s (%s): %s", tid, artist, exc)
            if not result["error"]:
                result["error"] = f"Chords: {exc}"

    # 3. Mood v2
    if needs_mood:
        try:
            tl = mood_timeline.analyse_track(tid, conn, audio_path=audio_path)
            if tl is not None:
                result["mood"] = True
        except Exception as exc:
            log.warning("Mood v2 failed for %s (%s): %s", tid, artist, exc)
            if not result["error"]:
                result["error"] = f"Mood: {exc}"

    result["elapsed_s"] = time.time() - t0
    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Number of candidate tracks to process (default: 25, 0 = unlimited)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Delay in seconds between track downloads (default: 0.5s)",
    )
    parser.add_argument("--skip-chords", action="store_true", help="Skip chord detection")
    parser.add_argument("--skip-moods", action="store_true", help="Skip mood timeline")
    parser.add_argument(
        "--max-per-artist",
        type=int,
        default=25,
        help="Max tracks per artist in one run, to spread a batch (default: 25, 0 = unlimited)",
    )
    parser.add_argument(
        "--exclude-artist",
        action="append",
        default=[],
        metavar="NAME",
        help="Skip tracks whose artist contains NAME, case-insensitive (repeatable)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview candidate tracks only")
    args = parser.parse_args(argv)

    print("=== KARAOKE LIBRARY GAP FILL: CHORDS V2 & MOOD V2 ===")
    with localcache.connect() as conn:
        candidates = fetch_candidates(
            conn,
            limit=args.limit if args.limit > 0 else None,
            exclude_artists=args.exclude_artist,
            max_per_artist=args.max_per_artist,
        )
        total = len(candidates)
        print(f"Found {total} candidate track(s) needing chords v2 or mood v2.")
        if args.dry_run:
            for i, c in enumerate(candidates[:20], 1):
                kinds = ",".join(s.get("kind", "") for s in (c.get("sources") or []))
                print(f"  {i:>2}. [{c['track_id']:>5}] {c['artist']} - {c['title']} ({kinds})")
            if total > 20:
                print(f"  ... and {total - 20} more.")
            return 0

        chords_ok = 0
        mood_ok = 0
        downloads = 0
        errors = 0

        for i, c in enumerate(candidates, 1):
            res = process_candidate(
                c,
                conn,
                skip_chords=args.skip_chords,
                skip_moods=args.skip_moods,
            )
            chords_tag = "✓ chords" if res["chords"] else "- chords"
            mood_tag = "✓ mood" if res["mood"] else "- mood"
            dl_tag = "(dl)" if res["downloaded"] else "(cached)"
            err_tag = f" [ERR: {res['error']}]" if res["error"] else ""

            if res["chords"]:
                chords_ok += 1
            if res["mood"]:
                mood_ok += 1
            if res["downloaded"]:
                downloads += 1
            if res["error"] and not (res["chords"] or res["mood"]):
                errors += 1

            print(
                f"[{i:>3}/{total}] {res['track_id']:>5} {res['artist'][:22]:22} - {res['title'][:25]:25} "
                f"{dl_tag:8} {chords_tag} | {mood_tag} ({res['elapsed_s']:.1f}s){err_tag}"
            )
            if res["downloaded"] and args.delay > 0:
                time.sleep(args.delay)

    print(
        f"\nSummary: {total} tracks processed | Chords v2: {chords_ok} | "
        f"Moods v2: {mood_ok} | Downloaded: {downloads} | Errors: {errors}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
