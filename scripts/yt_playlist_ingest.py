#!/usr/bin/env python3
"""Ingest a YouTube Music playlist into the karaoke library, then vectorise it.

Flow
----
1. Fetch the playlist tracks from YouTube Music (by playlist ID or URL).
2. For each track: download the audio into the YT cache (``youtube_dir``),
   resolve metadata, and upsert into SQLite via ``add_track_source``.
3. Run ``process_youtube_cache`` over the cache directory — this handles
   Key/BPM analysis, CLAP embedding, genre labelling, and postprocess enqueueing
   for every file in the cache (not just the ones just downloaded, so stale
   gaps are filled as a side-effect).

Usage
-----
    # Ingest a playlist by ID:
    python scripts/yt_playlist_ingest.py PLxxxxxxxxxxxxxxxxxxxx

    # Ingest by full URL:
    python scripts/yt_playlist_ingest.py "https://music.youtube.com/playlist?list=PLxxx"

    # Dry-run (list tracks, no download or DB write):
    python scripts/yt_playlist_ingest.py PLxxx --dry-run

    # Skip the CLAP / genre pass (faster, just download + Key/BPM):
    python scripts/yt_playlist_ingest.py PLxxx --no-vectors

    # Skip re-downloading tracks already in the YT cache:
    python scripts/yt_playlist_ingest.py PLxxx --skip-cached
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Suppress noisy tokenizer / tqdm output from the audio stack.
# ---------------------------------------------------------------------------
import os
os.environ.setdefault("TQDM_DISABLE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from karaoke import cache_ingest, localcache
from karaoke.config import settings
from karaoke.logger import log
from karaoke.ytmusic_client import YTMusicClient, YTMusicAuthError

_PLAYLIST_ID_RE = re.compile(r"(?:list=|^)((?:PL|RDCLAK|OLAK|VL)[A-Za-z0-9_-]{8,}|[A-Za-z0-9_-]{12,})")


def extract_playlist_id(value: str) -> str:
    """Extract a YouTube playlist ID from a raw ID or a full URL."""
    value = value.strip().strip("'\"")
    m = _PLAYLIST_ID_RE.search(value)
    if m:
        return m.group(1)
    # Bare ID without prefix
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", value):
        return value
    return ""


def fetch_playlist_tracks(playlist_id: str) -> list[dict]:
    """Return a list of {videoId, title, artist} dicts from the playlist."""
    try:
        yt = YTMusicClient()
        if not yt.is_authenticated:
            print("⚠  YouTube Music is not authenticated — fetching playlist metadata "
                  "via yt-dlp instead (public playlists only).")
            return _fetch_via_ytdlp(playlist_id)
        raw = yt.get_playlist(playlist_id, limit=500)
    except YTMusicAuthError:
        print("⚠  YouTube Music auth failed — falling back to yt-dlp.")
        return _fetch_via_ytdlp(playlist_id)
    except Exception as exc:
        print(f"⚠  YTMusic API error ({exc}) — falling back to yt-dlp.")
        return _fetch_via_ytdlp(playlist_id)

    tracks = []
    for t in (raw.get("tracks") or []):
        vid = str(t.get("videoId") or "").strip()
        if not vid:
            continue
        artists = t.get("artists") or []
        artist = str(artists[0].get("name") if artists and isinstance(artists[0], dict) else "").strip()
        title = str(t.get("title") or "").strip()
        tracks.append({"videoId": vid, "title": title, "artist": artist})
    return tracks


def _fetch_via_ytdlp(playlist_id: str) -> list[dict]:
    """Flat-extract playlist metadata without downloading audio."""
    try:
        from yt_dlp import YoutubeDL  # type: ignore
    except ImportError:
        print("✗ yt-dlp is not installed. Install it: pip install yt-dlp")
        sys.exit(1)

    url = f"https://www.youtube.com/playlist?list={playlist_id}"
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}

    tracks = []
    for entry in (info.get("entries") or []):
        vid = entry.get("id") or ""
        if not vid:
            continue
        raw_title = entry.get("title") or ""
        # YT often puts "Artist - Title" in the title; split naively.
        if " - " in raw_title:
            artist, title = raw_title.split(" - ", 1)
        else:
            artist, title = "", raw_title
        tracks.append({
            "videoId": vid,
            "title": title.strip(),
            "artist": artist.strip(),
        })
    return tracks


def cached_video_ids(yt_dir: Path) -> set[str]:
    """Video IDs that are already on disk in the YT cache directory."""
    suffixes = {".webm", ".m4a", ".mp4", ".mkv", ".opus", ".ogg", ".mp3", ".flac"}
    return {
        f.stem
        for f in yt_dir.glob("*")
        if f.is_file() and f.suffix.lower() in suffixes
    }


def download_audio(video_id: str, yt_dir: Path) -> Optional[str]:
    """Download audio for a single video ID into yt_dir.  Returns the local path."""
    try:
        from yt_dlp import YoutubeDL  # type: ignore
    except ImportError:
        raise RuntimeError("yt-dlp is not installed")

    url = f"https://www.youtube.com/watch?v={video_id}"
    yt_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": str(yt_dir / "%(id)s.%(ext)s"),
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = ydl.prepare_filename(info)
    return path if path and Path(path).is_file() else None


def _progress(stage: str, i: int, total: int, msg: str) -> None:
    tag = {"ingest": "📥", "analysis": "🎵", "clap": "🧠", "genre": "🏷 "}.get(stage, "▸")
    print(f"  {tag} [{i:3d}/{total}] {msg}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("playlist", help="YouTube Music playlist ID or URL")
    ap.add_argument("--dry-run", action="store_true",
                    help="List playlist tracks and exit without downloading or writing to DB")
    ap.add_argument("--skip-cached", action="store_true",
                    help="Skip downloading tracks already present in the YT cache")
    ap.add_argument("--no-vectors", action="store_true",
                    help="Skip CLAP embedding and genre classification (Key/BPM still runs)")
    ap.add_argument("--no-analysis", action="store_true",
                    help="Skip Key/BPM/Energy analysis")
    ap.add_argument("--force-harmony", action="store_true",
                    help="Re-run chord + key-progression analysis even for tracks already indexed")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process at most N tracks from the playlist")
    args = ap.parse_args(argv)

    playlist_id = extract_playlist_id(args.playlist)
    if not playlist_id:
        print(f"✗ Could not extract a playlist ID from: {args.playlist!r}")
        return 1

    print(f"\n{'='*60}")
    print(f"  YT PLAYLIST INGEST  —  {playlist_id}")
    print(f"{'='*60}\n")

    # ── 1. Fetch playlist tracks ──────────────────────────────────────────────
    print("📋 Fetching playlist tracks from YouTube Music…")
    tracks = fetch_playlist_tracks(playlist_id)
    if not tracks:
        print("✗ No tracks found in playlist.")
        return 1

    if args.limit:
        tracks = tracks[: args.limit]

    print(f"  Found {len(tracks)} track(s).\n")

    if args.dry_run:
        print("DRY-RUN — tracks that would be ingested:\n")
        for i, t in enumerate(tracks, 1):
            print(f"  {i:3d}. {t['artist'] or '?'} — {t['title'] or '?'}  (id: {t['videoId']})")
        print()
        return 0

    # ── 2. Download audio ─────────────────────────────────────────────────────
    yt_dir = Path(settings.youtube_dir).expanduser()
    already_cached = cached_video_ids(yt_dir) if args.skip_cached else set()

    downloaded = 0
    skipped = 0
    failed_dl = 0

    print("⬇  Downloading audio…\n")
    for i, track in enumerate(tracks, 1):
        vid = track["videoId"]
        label = f"{track['artist'] or '?'} — {track['title'] or '?'}"

        if vid in already_cached:
            print(f"  ⏭  [{i:3d}/{len(tracks)}] {label}  (already cached)")
            skipped += 1
            continue

        print(f"  ⬇  [{i:3d}/{len(tracks)}] {label}…", end=" ", flush=True)
        t0 = time.time()
        try:
            path = download_audio(vid, yt_dir)
            if path:
                elapsed = time.time() - t0
                print(f"✓  {elapsed:.1f}s")
                downloaded += 1
            else:
                print("✗  no file produced")
                failed_dl += 1
        except Exception as exc:
            print(f"✗  {exc}")
            failed_dl += 1

    print(f"\n  Downloaded: {downloaded}  |  Skipped (cached): {skipped}  |  Failed: {failed_dl}\n")

    # ── 3. Run cache ingestion pipeline ──────────────────────────────────────
    #  process_youtube_cache is idempotent — it walks *all* files in the cache
    #  directory and skips anything already indexed, so it naturally fills any
    #  gaps left by previous runs too.
    print("🔄 Running cache ingestion pipeline (metadata → analysis → vectors)…\n")

    do_clap = not args.no_vectors
    do_analysis = not args.no_analysis

    stats = cache_ingest.process_youtube_cache(
        yt_dir=yt_dir,
        embed_clap=do_clap,
        classify_genres=do_clap,          # genre classification requires CLAP
        analyze_audio_features=do_analysis,
        enqueue_postprocessing=False,
        progress_callback=_progress,
    )

    # ── 4. Chord + key-progression analysis ──────────────────────────────────
    #  cache_ingest does not run harmony analysis (it lives in folder_scan).
    #  We replicate the same pattern here: skip tracks already in OpenSearch,
    #  then run key_progression.analyse + chords.detect on each downloaded file.
    progressions_done = 0
    chords_done = 0
    harmony_errors = 0

    if do_analysis:
        print("\n🎹 Running harmony analysis (key progressions + chords)…\n")
        try:
            from karaoke import key_progression, chords as chords_mod
            from karaoke.osclient import client as os_client

            osc = os_client()
            # Which track_ids already have a progression / chord document?
            done_prog: set[int] = set()
            done_chords: set[int] = set()
            if not args.force_harmony:
                try:
                    res = osc.search(index=key_progression.PROGRESSION_INDEX, body={
                        "size": 10000, "_source": ["track_id"],
                        "query": {"match_all": {}},
                    })
                    done_prog = {int(h["_source"]["track_id"]) for h in res["hits"]["hits"]}
                    res2 = osc.search(index=key_progression.PROGRESSION_INDEX, body={
                        "size": 10000, "_source": ["track_id"],
                        "query": {"exists": {"field": "chord_motion"}},
                    })
                    done_chords = {int(h["_source"]["track_id"]) for h in res2["hits"]["hits"]}
                except Exception:
                    pass  # index may not exist yet; scan everything
            else:
                print("  ⚡ --force-harmony: skipping existing index check, reanalysing all files.")

            # Build a vid→track_id map from DB for the playlist's video IDs
            conn_h = localcache.connect()
            vid_to_tid: dict[str, int] = {}
            try:
                for track in tracks:
                    vid = track["videoId"]
                    url = f"https://www.youtube.com/watch?v={vid}"
                    row = conn_h.execute(
                        "SELECT track_id FROM sources WHERE url = %s LIMIT 1", (url,)
                    ).fetchone()
                    if row:
                        vid_to_tid[vid] = int(row["track_id"])
            finally:
                conn_h.close()

            # Only analyse files that belong to this playlist
            files_to_analyse = [
                f for f in sorted(yt_dir.glob("*.*"))
                if f.is_file()
                and f.suffix.lower() in {".webm", ".m4a", ".mp4", ".mkv", ".opus", ".ogg", ".mp3", ".flac"}
                and f.stem in vid_to_tid
            ]

            for i, file_path in enumerate(files_to_analyse, 1):
                tid = vid_to_tid[file_path.stem]
                needs_prog = tid not in done_prog
                needs_chords = tid not in done_chords
                if not needs_prog and not needs_chords:
                    continue

                # Try to get artist/title for logging
                label = file_path.stem
                conn_h2 = localcache.connect()
                try:
                    row = conn_h2.execute(
                        "SELECT artist, title FROM tracks WHERE track_id = %s", (tid,)
                    ).fetchone()
                    if row:
                        label = f"{row['artist']} — {row['title']}"
                finally:
                    conn_h2.close()

                print(f"  🎼 [{i:3d}/{len(files_to_analyse)}] {label}…", end=" ", flush=True)
                try:
                    prog = key_progression.analyse(str(file_path)) if needs_prog else None
                    chord_analysis = None
                    if needs_chords:
                        try:
                            chord_analysis = chords_mod.detect(str(file_path))
                        except Exception:
                            pass

                    parts = label.split(" — ", 1)
                    artist_label = parts[0] if len(parts) > 1 else ""
                    title_label = parts[-1]

                    if prog and key_progression.store(
                        tid, prog,
                        artist=artist_label,
                        title=title_label,
                        chords=chord_analysis,
                    ):
                        progressions_done += 1
                        done_prog.add(tid)
                        if chord_analysis is not None:
                            chords_done += 1
                            done_chords.add(tid)
                        print("✓")
                    else:
                        print("–")
                except Exception as exc:
                    print(f"✗ {exc}")
                    harmony_errors += 1

        except Exception as exc:
            print(f"  ⚠ Harmony pass skipped: {exc}")

    print(f"""
{'='*60}
  INGEST COMPLETE
{'='*60}
  Playlist tracks   : {len(tracks)}
  Downloaded        : {downloaded}
  Already cached    : {skipped}
  Download failures : {failed_dl}

  Cache pipeline (all files in cache dir):
    Total files       : {stats.total_files}
    New tracks added  : {stats.new_tracks}
    Audio analysed    : {stats.analyzed}
    CLAP embedded     : {stats.clap_embedded}
    Genres labelled   : {stats.genres_labeled}
    Postprocess queued: {stats.enqueued}
    Errors            : {stats.errors}

  Harmony (playlist files only):
    Key progressions  : {progressions_done}
    Chord analyses    : {chords_done}
    Errors            : {harmony_errors}
{'='*60}
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
