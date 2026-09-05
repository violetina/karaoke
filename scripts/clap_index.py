"""Embed cached library audio with CLAP.

Companion to ``vectorize_cached.py``, which fills the 62-dimension spectral
index. Both describe the same audio and answer different questions: the
spectral vector is cheap and model-free, this one shares a space with text.

Roughly three seconds per track on CPU, plus an ffmpeg transcode, so the whole
cached library is a few minutes. Re-running replaces rather than accumulates:
one embedding per track, because this describes the music rather than a
particular capture of it.
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from karaoke import clap_vector, localcache
from karaoke.config import settings
from karaoke.logger import log

MEDIA_SUFFIXES = (".webm", ".m4a", ".mp4", ".mkv", ".opus", ".ogg", ".mp3")


def cached_media() -> dict[str, Path]:
    directory = Path(settings.youtube_dir)
    if not directory.is_dir():
        return {}
    found: dict[str, Path] = {}
    for path in sorted(directory.glob("*")):
        if path.is_file() and path.suffix in MEDIA_SUFFIXES:
            found.setdefault(path.stem, path)
    return found


def already_embedded(client) -> set[int]:
    try:
        res = client.search(index=clap_vector.CLAP_INDEX, body={
            "size": 10000, "_source": ["track_id"], "query": {"match_all": {}}})
    except Exception:
        return set()
    return {h["_source"]["track_id"] for h in res["hits"]["hits"]}


def run(*, limit: Optional[int] = None, overwrite: bool = False,
        dry_run: bool = False) -> int:
    if not clap_vector.available():
        print("CLAP is unavailable: torch and transformers are required.")
        return 1

    from karaoke.osclient import client as os_client

    conn = localcache.connect()
    media = cached_media()
    print(f"{len(media)} cached audio file(s)")

    client = None
    done: set[int] = set()
    if not dry_run:
        client = os_client()
        clap_vector.ensure_index(client)
        done = set() if overwrite else already_embedded(client)
        print(f"{len(done)} track(s) already embedded")

    rows = conn.execute(
        "SELECT DISTINCT t.track_id, t.artist, t.title, t.album, s.url,"
        "       a.detected_key, a.bpm"
        "  FROM tracks t"
        "  JOIN sources s ON s.track_id = t.track_id"
        "  LEFT JOIN track_analysis a ON a.track_id = t.track_id"
        " WHERE s.url LIKE '%youtu%'"
        " ORDER BY t.artist, t.title").fetchall()

    made = skipped = failed = 0
    for row in rows:
        if limit and made >= limit:
            break
        vid = localcache.extract_youtube_id(row["url"] or "")
        source_file = media.get(vid) if vid else None
        if source_file is None:
            continue
        track_id = int(row["track_id"])
        if track_id in done:
            skipped += 1
            continue

        label = f"{(row['artist'] or '')[:22]:24} {(row['title'] or '')[:26]:28}"
        if dry_run:
            print(f"  {label} would embed {source_file.name}")
            made += 1
            continue

        t0 = time.time()
        vector = clap_vector.embed_audio(str(source_file))
        if vector is None:
            print(f"  {label} could not embed")
            failed += 1
            continue
        try:
            client.index(
                index=clap_vector.CLAP_INDEX,
                id=clap_vector.doc_id(track_id),
                body=clap_vector.build_doc(
                    track_id=track_id, artist=row["artist"] or "",
                    title=row["title"] or "", album=row["album"] or "",
                    vector=vector,
                    embedded_at=datetime.now(timezone.utc).isoformat(),
                    detected_key=row["detected_key"] or "", bpm=row["bpm"]),
            )
        except Exception as exc:
            print(f"  {label} index failed: {exc}")
            failed += 1
            continue
        made += 1
        print(f"  {label} {time.time() - t0:5.1f}s")

    if client is not None:
        client.indices.refresh(index=clap_vector.CLAP_INDEX)
        total = client.count(index=clap_vector.CLAP_INDEX)["count"]
        print(f"\n{made} embedded, {skipped} already had one, {failed} failed")
        print(f"{clap_vector.CLAP_INDEX} now holds {total} document(s)")
    else:
        print(f"\n{made} would be embedded (dry run)")
    conn.close()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    log.info("embedding cached audio with CLAP")
    return run(limit=args.limit, overwrite=args.overwrite, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
