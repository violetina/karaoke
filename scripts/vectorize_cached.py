"""Give library tracks a sound vector, from audio already on disk.

Audio vectors existed only as a side effect of record mode:
``_index_audio_vector`` is called from ``recording_worker`` and nowhere else, so
"sounds like" search covered whichever tracks happened to be captured -- 133
documents against a library of 763 -- and quietly returned nothing useful for
the rest.

The cached YouTube audio is the obvious place to start, because it is already
downloaded and needs no network. Those files are audio-only (no video stream,
which is why cover art could not be taken from them) but that is irrelevant
here: every feature in the vector is spectral or pitch-class, and none of them
wants a picture.

Documents are written with ``source: library`` so they stay distinguishable
from ``source: recording``. Both are kept for a track that has each: a recording
describes one performance heard through a speaker and a monitor capture, while
the cached file is the release itself, and they are legitimately different
observations of the same song.
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from karaoke import audio_vector, localcache
from karaoke.config import settings
from karaoke.logger import log

# Distinguishes these from record-mode documents, which describe a performance
# rather than the release.
SOURCE = "library"

# Cached media is audio-only; the extensions the downloader actually writes.
MEDIA_SUFFIXES = (".webm", ".m4a", ".mp4", ".mkv", ".opus", ".ogg", ".mp3")


def cached_media() -> dict[str, Path]:
    """Cached audio by video id."""
    directory = Path(settings.youtube_dir)
    if not directory.is_dir():
        return {}
    found: dict[str, Path] = {}
    for path in sorted(directory.glob("*")):
        if path.is_file() and path.suffix in MEDIA_SUFFIXES:
            found.setdefault(path.stem, path)
    return found


def library_doc_id(track_id: int) -> str:
    """One document per track for the release itself.

    Unlike a recording, which is keyed by start time because the same song
    heard twice is two observations, there is only ever one cached file per
    track -- so re-running replaces rather than accumulates.
    """
    return f"audio:library:{track_id}"


def already_indexed(client) -> set[int]:
    """Track ids that already have a library-sourced vector."""
    try:
        res = client.search(index=audio_vector.AUDIO_INDEX, body={
            "size": 10000, "_source": ["track_id"],
            "query": {"term": {"source": SOURCE}}})
    except Exception:
        return set()
    return {h["_source"]["track_id"] for h in res["hits"]["hits"]}


def run(*, limit: Optional[int] = None, overwrite: bool = False,
        dry_run: bool = False) -> int:
    from karaoke.osclient import client as os_client

    conn = localcache.connect()
    media = cached_media()
    print(f"{len(media)} cached audio file(s)")

    client = None
    done: set[int] = set()
    if not dry_run:
        client = os_client()
        audio_vector.ensure_audio_index(client)
        done = set() if overwrite else already_indexed(client)
        print(f"{len(done)} track(s) already have a library vector")

    rows = conn.execute(
        "SELECT DISTINCT t.track_id, t.artist, t.title, t.duration, s.url,"
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
            print(f"  {label} would vectorise {source_file.name}")
            made += 1
            continue

        t0 = time.time()
        vector = audio_vector.extract(str(source_file))
        if vector is None:
            print(f"  {label} could not extract")
            failed += 1
            continue

        try:
            client.index(
                index=audio_vector.AUDIO_INDEX,
                id=library_doc_id(track_id),
                body=audio_vector.build_audio_doc(
                    track_id=track_id,
                    # No recording produced this one. 0 rather than None keeps
                    # the field an integer for anything that aggregates on it.
                    recording_id=0,
                    artist=row["artist"] or "", title=row["title"] or "",
                    vector=vector,
                    recorded_at=datetime.now(timezone.utc).isoformat(),
                    duration_s=float(row["duration"] or 0.0),
                    detected_key=row["detected_key"] or "",
                    bpm=row["bpm"],
                    source=SOURCE),
            )
        except Exception as exc:
            print(f"  {label} index failed: {exc}")
            failed += 1
            continue

        made += 1
        print(f"  {label} {time.time() - t0:5.1f}s")

    if client is not None:
        client.indices.refresh(index=audio_vector.AUDIO_INDEX)
        total = client.count(index=audio_vector.AUDIO_INDEX)["count"]
        print(f"\n{made} vectorised, {skipped} already had one, {failed} failed")
        print(f"{audio_vector.AUDIO_INDEX} now holds {total} document(s)")
    else:
        print(f"\n{made} would be vectorised (dry run)")
    conn.close()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--overwrite", action="store_true",
                    help="re-extract tracks that already have a library vector")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    log.info("vectorising cached library audio")
    return run(limit=args.limit, overwrite=args.overwrite, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
