"""Library scanner: walk a music dir -> tags -> LRCLIB lyrics -> embed -> index.

Idempotent: each track's OpenSearch _id is a stable hash of its path, so
re-scanning upserts instead of duplicating. `--force` re-fetches lyrics even for
tracks already indexed with lyrics.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import settings
from .tags import TrackTags, extract_tags, is_audio


def doc_id(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8")).hexdigest()


def iter_audio_files(root: Path) -> Iterator[Path]:
    for p in sorted(root.rglob("*")):
        if p.is_file() and is_audio(p):
            yield p


def _embed_source(tags: TrackTags, plain_lyrics: str) -> str:
    """Text used for the semantic vector: lyrics if present, else metadata."""
    if plain_lyrics.strip():
        return plain_lyrics
    return f"{tags.title} {tags.artist} {tags.album}".strip()


@dataclass
class ScanStats:
    seen: int = 0
    indexed: int = 0
    skipped: int = 0
    with_synced: int = 0
    errors: int = 0


def build_doc(
    tags: TrackTags,
    *,
    source: str = "local",
    fetch_lyrics: bool = True,
    embed: bool = True,
) -> dict[str, Any]:
    """Assemble the OpenSearch document for one track."""
    from .lyrics import fetch_lrclib

    plain = synced_raw = ""
    lyrics_source = "none"
    has_synced = False
    if fetch_lyrics and tags.artist and tags.title:
        ly = fetch_lrclib(tags.artist, tags.title, tags.album, tags.duration)
        plain, synced_raw = ly.plain, ly.synced_raw
        lyrics_source = ly.source
        has_synced = ly.has_synced

    doc: dict[str, Any] = {
        "path": tags.path,
        "title": tags.title,
        "artist": tags.artist,
        "album": tags.album,
        "year": tags.year,
        "duration": tags.duration,
        "source": source,
        "has_synced": has_synced,
        "lyrics_source": lyrics_source,
        "plain_lyrics": plain,
        "synced_lyrics": synced_raw,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    if embed:
        from .embed import embed_text

        doc["lyrics_vector"] = embed_text(_embed_source(tags, plain))
    return doc


def _already_good(os_client: Any, _id: str) -> bool:
    """True if the doc exists and already has lyrics (skip unless --force)."""
    try:
        res = os_client.get(index=settings.index_name, id=_id)
        src = res.get("_source", {})
        return bool(src.get("lyrics_source") not in (None, "", "none"))
    except Exception:
        return False


def scan(
    music_dir: Optional[Path] = None,
    *,
    force: bool = False,
    limit: Optional[int] = None,
    rate_delay: float = 0.34,
    os_client: Any = None,
    progress: bool = True,
) -> ScanStats:
    """Scan the library and index tracks. Returns ScanStats."""
    from .osclient import client, ensure_index

    root = Path(music_dir or settings.music_dir).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"music dir not found: {root}")

    c = os_client or client()
    ensure_index(c)

    stats = ScanStats()
    for path in iter_audio_files(root):
        if limit is not None and stats.seen >= limit:
            break
        stats.seen += 1
        _id = doc_id(str(path))
        if not force and _already_good(c, _id):
            stats.skipped += 1
            if progress:
                print(f"  skip  {path.name}")
            continue
        try:
            tags = extract_tags(path)
            doc = build_doc(tags)
            c.index(index=settings.index_name, id=_id, body=doc)
            stats.indexed += 1
            if doc["has_synced"]:
                stats.with_synced += 1
            if progress:
                mark = "♪" if doc["has_synced"] else " "
                print(f"  {mark} idx  {tags.artist} - {tags.title}  [{doc['lyrics_source']}]")
            time.sleep(rate_delay)  # be polite to LRCLIB
        except Exception as e:  # noqa: BLE001 - keep scanning on per-file errors
            stats.errors += 1
            if progress:
                print(f"  ERR   {path.name}: {e}")

    c.indices.refresh(index=settings.index_name)
    return stats


def _cli() -> int:  # pragma: no cover - manual run
    import argparse

    ap = argparse.ArgumentParser(description="Scan a music library into OpenSearch")
    ap.add_argument("--dir", type=Path, default=None, help="music dir (default MUSIC_DIR)")
    ap.add_argument("--force", action="store_true", help="re-fetch even if already indexed")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    stats = scan(a.dir, force=a.force, limit=a.limit)
    print(
        f"\nseen={stats.seen} indexed={stats.indexed} skipped={stats.skipped} "
        f"synced={stats.with_synced} errors={stats.errors}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
