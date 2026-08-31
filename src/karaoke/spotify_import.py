"""Import Spotify saved-tracks metadata into the tracks index (metadata seed).

Spotify's API does not permit audio download, so these docs are metadata-only
(``source='spotify'``, no local ``path``): lyrics come from LRCLIB and the
semantic vector is built from lyrics (or title+artist fallback). They mark what
your library contains so you can see coverage and later match against local
files.

Input is the JSON array of ``items`` from ``GET /v1/me/tracks`` (the shape the
Hermes ``spotify_library`` tool returns). Read from a file, or pipe via stdin.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .config import settings
from .tags import TrackTags


def spotify_id_to_doc_id(track_id: str) -> str:
    """OpenSearch document id namespace for Spotify metadata-only tracks."""
    return "spotify:" + hashlib.sha1(track_id.encode("utf-8")).hexdigest()


def _year(release_date: str) -> Optional[int]:
    if release_date and release_date[:4].isdigit():
        return int(release_date[:4])
    return None


def track_to_tags(track: dict[str, Any]) -> TrackTags:
    """Convert one Spotify track object into TrackTags (path = spotify URI)."""
    artists = track.get("artists") or []
    artist = ", ".join(a.get("name", "") for a in artists if a.get("name"))
    album = (track.get("album") or {}).get("name", "")
    rel = (track.get("album") or {}).get("release_date", "")
    dur_ms = track.get("duration_ms")
    return TrackTags(
        path=track.get("uri", track.get("id", "")),
        title=track.get("name", ""),
        artist=artist,
        album=album,
        year=_year(rel),
        duration=(dur_ms / 1000.0) if dur_ms else None,
    )


def iter_tracks(items: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    """Yield the inner track object from saved-tracks ``items`` entries."""
    for it in items:
        track = it.get("track") if "track" in it else it
        if track and track.get("id"):
            yield track


def import_items(
    items: list[dict[str, Any]],
    *,
    fetch_lyrics: bool = True,
    embed: bool = True,
    rate_delay: float = 0.34,
    limit: Optional[int] = None,
    os_client: Any = None,
    progress: bool = True,
) -> dict[str, int]:
    """Index Spotify tracks as metadata docs. Returns counts."""
    from .osclient import client, ensure_index
    from .scanner import build_doc

    c = os_client or client()
    ensure_index(c)

    counts = {"seen": 0, "indexed": 0, "with_synced": 0, "errors": 0}
    for track in iter_tracks(items):
        if limit is not None and counts["seen"] >= limit:
            break
        counts["seen"] += 1
        try:
            tags = track_to_tags(track)
            doc = build_doc(tags, source="spotify",
                            fetch_lyrics=fetch_lyrics, embed=embed)
            _id = spotify_id_to_doc_id(track["id"])
            c.index(index=settings.index_name, id=_id, body=doc)
            counts["indexed"] += 1
            if doc["has_synced"]:
                counts["with_synced"] += 1
            if progress:
                mark = "♪" if doc["has_synced"] else " "
                print(f"  {mark} {tags.artist} - {tags.title}  [{doc['lyrics_source']}]")
            if fetch_lyrics:
                time.sleep(rate_delay)
        except Exception as e:  # noqa: BLE001
            counts["errors"] += 1
            if progress:
                print(f"  ERR  {track.get('name','?')}: {e}")

    c.indices.refresh(index=settings.index_name)
    return counts


def _cli() -> int:  # pragma: no cover - manual run
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Import Spotify saved tracks (JSON) into OpenSearch")
    ap.add_argument("json_file", nargs="?", help="file with saved-tracks items array (default: stdin)")
    ap.add_argument("--no-lyrics", action="store_true", help="metadata only, skip LRCLIB + embed")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()

    raw = open(a.json_file).read() if a.json_file else sys.stdin.read()
    data = json.loads(raw)
    items = data.get("items", data) if isinstance(data, dict) else data

    counts = import_items(
        items,
        fetch_lyrics=not a.no_lyrics,
        embed=not a.no_lyrics,
        limit=a.limit,
    )
    print(f"\nseen={counts['seen']} indexed={counts['indexed']} "
          f"synced={counts['with_synced']} errors={counts['errors']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
