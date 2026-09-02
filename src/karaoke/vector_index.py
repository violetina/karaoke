"""Build derived OpenSearch vector indexes from the local SQLite database.

SQLite remains the operational source of truth. This module reads approved tracks,
sources and lyrics from SQLite and writes rebuildable documents to OpenSearch for
semantic search and future training features.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from . import localcache
from .config import settings
from .lyrics import parse_lrc


@dataclass
class VectorIndexStats:
    """Counters returned by a SQLite -> OpenSearch indexing run."""

    seen: int = 0
    indexed: int = 0
    skipped: int = 0
    errors: int = 0
    line_docs: int = 0


def track_doc_id(track_id: int) -> str:
    """Stable OpenSearch id for a SQLite-backed track document."""
    return f"sqlite:{track_id}"


def line_doc_id(track_id: int, line_index: int) -> str:
    """Stable OpenSearch id for a SQLite-backed lyric-line document."""
    return f"sqlite-line:{track_id}:{line_index}"


def _embedding_text(row: Any) -> str:
    plain = row["plain_lyrics"] or ""
    if plain.strip():
        return plain
    return f"{row['title']} {row['artist']} {row['album'] or ''}".strip()


def iter_track_rows(conn: Any) -> Iterable[Any]:
    """Yield one row per SQLite track with preferred source and approved lyrics."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            t.track_id,
            t.artist,
            t.title,
            t.album,
            t.duration,
            s.kind AS source_kind,
            s.url AS source_url,
            s.player_name,
            l.source AS lyrics_source,
            l.synced_lyrics,
            l.plain_lyrics
        FROM tracks t
        LEFT JOIN sources s ON s.source_id = (
            SELECT source_id FROM sources
            WHERE track_id = t.track_id
            ORDER BY CASE kind
                WHEN 'youtube' THEN 0
                WHEN 'spotify' THEN 1
                WHEN 'local' THEN 2
                ELSE 3
            END, source_id
            LIMIT 1
        )
        LEFT JOIN lyrics l ON l.lyric_id = (
            SELECT lyric_id FROM lyrics
            WHERE track_id = t.track_id AND kind = 'approved'
            ORDER BY lyric_id DESC
            LIMIT 1
        )
        ORDER BY t.artist, t.title
        """
    )
    yield from cur.fetchall()


def build_track_doc(row: Any, *, embed: bool = True) -> dict[str, Any]:
    """Build one OpenSearch track document from a SQLite query row."""
    synced = row["synced_lyrics"] or ""
    plain = row["plain_lyrics"] or ""
    lines = parse_lrc(synced) if synced else []
    doc: dict[str, Any] = {
        "track_id": row["track_id"],
        "path": row["source_url"] or "",
        "source_url": row["source_url"] or "",
        "source_kind": row["source_kind"] or "sqlite",
        "player_name": row["player_name"] or "",
        "title": row["title"] or "",
        "artist": row["artist"] or "",
        "album": row["album"] or "",
        "duration": row["duration"],
        "source": "sqlite",
        "has_synced": bool(lines),
        "lyrics_source": row["lyrics_source"] or "none",
        "plain_lyrics": plain,
        "synced_lyrics": synced,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    if embed:
        from .embed import embed_text

        doc["lyrics_vector"] = embed_text(_embedding_text(row))
    return doc


def build_line_docs(row: Any, *, embed: bool = True) -> list[tuple[str, dict[str, Any]]]:
    """Build line-level documents for future semantic/timing experiments."""
    synced = row["synced_lyrics"] or ""
    parsed = parse_lrc(synced) if synced else []
    if not parsed:
        return []
    docs: list[tuple[str, dict[str, Any]]] = []
    texts: list[str] = []
    base_docs: list[dict[str, Any]] = []
    for i, (start_s, text) in enumerate(parsed):
        end_s = parsed[i + 1][0] if i + 1 < len(parsed) else None
        previous_text = parsed[i - 1][1] if i else ""
        next_text = parsed[i + 1][1] if i + 1 < len(parsed) else ""
        context = "\n".join(t for t in (previous_text, text, next_text) if t)
        doc = {
            "track_id": row["track_id"],
            "line_index": i,
            "artist": row["artist"] or "",
            "title": row["title"] or "",
            "start_s": start_s,
            "end_s": end_s,
            "duration_s": (end_s - start_s) if end_s is not None else None,
            "text": text,
            "context": context,
            "source": "sqlite-line",
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }
        docs.append((line_doc_id(row["track_id"], i), doc))
        texts.append(context or text)
        base_docs.append(doc)
    if embed:
        from .embed import embed_batch

        for doc, vec in zip(base_docs, embed_batch(texts)):
            doc["line_vector"] = vec
    return docs


def ensure_line_index(os_client: Any, index_name: str) -> bool:
    """Create the line-level vector index if absent."""
    if os_client.indices.exists(index=index_name):
        return False
    body = {
        "settings": {"index": {"knn": True, "number_of_replicas": 0}},
        "mappings": {
            "properties": {
                "track_id": {"type": "integer"},
                "line_index": {"type": "integer"},
                "artist": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
                "title": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
                "start_s": {"type": "float"},
                "end_s": {"type": "float"},
                "duration_s": {"type": "float"},
                "text": {"type": "text"},
                "context": {"type": "text"},
                "source": {"type": "keyword"},
                "indexed_at": {"type": "date"},
                "line_vector": {
                    "type": "knn_vector",
                    "dimension": settings.embed_dim,
                    "method": {
                        "name": "hnsw",
                        "space_type": "cosinesimil",
                        "engine": "lucene",
                    },
                },
            }
        },
    }
    os_client.indices.create(index=index_name, body=body)
    return True


def rebuild_from_sqlite(
    *,
    db_path: Optional[str] = None,
    limit: Optional[int] = None,
    embed: bool = True,
    include_lines: bool = False,
    dry_run: bool = False,
    os_client: Any = None,
) -> VectorIndexStats:
    """Index SQLite tracks into OpenSearch; safe to re-run."""
    from .osclient import client, ensure_index

    conn = localcache.connect(None if db_path is None else Path(db_path))
    stats = VectorIndexStats()
    try:
        rows = list(iter_track_rows(conn))
    finally:
        conn.close()

    if limit is not None:
        rows = rows[:limit]

    c = os_client if os_client is not None else (None if dry_run else client())
    if c is not None:
        ensure_index(c)
        if include_lines:
            ensure_line_index(c, f"{settings.index_name}-lines")

    for row in rows:
        stats.seen += 1
        try:
            doc = build_track_doc(row, embed=embed)
            if dry_run:
                stats.skipped += 1
            else:
                assert c is not None
                c.index(index=settings.index_name, id=track_doc_id(row["track_id"]), body=doc)
                stats.indexed += 1
            if include_lines:
                for _doc_id, line_doc in build_line_docs(row, embed=embed):
                    stats.line_docs += 1
                    if not dry_run:
                        assert c is not None
                        c.index(index=f"{settings.index_name}-lines", id=_doc_id, body=line_doc)
        except Exception:
            stats.errors += 1
    if c is not None and not dry_run:
        c.indices.refresh(index=settings.index_name)
        if include_lines:
            c.indices.refresh(index=f"{settings.index_name}-lines")
    return stats


def vector_index_main(argv: Optional[list[str]] = None) -> int:
    """CLI entrypoint for rebuilding derived vector indexes from SQLite."""
    ap = argparse.ArgumentParser(
        prog="karaoke-vector-index",
        description="Rebuild optional OpenSearch vector indexes from the SQLite DB",
    )
    ap.add_argument("--rebuild", action="store_true", help="index SQLite tracks into OpenSearch")
    ap.add_argument("--lines", action="store_true", help="also build a line-level index")
    ap.add_argument("--no-embed", action="store_true", help="skip embedding generation")
    ap.add_argument("--dry-run", action="store_true", help="read/build docs without writing OpenSearch")
    ap.add_argument("--limit", type=int, default=None, help="maximum tracks to process")
    ap.add_argument("--db", default=None, help="SQLite DB path (default: configured local DB)")
    args = ap.parse_args(argv)

    if not args.rebuild and not args.dry_run:
        ap.error("choose --rebuild or --dry-run")

    stats = rebuild_from_sqlite(
        db_path=args.db,
        limit=args.limit,
        embed=not args.no_embed,
        include_lines=args.lines,
        dry_run=args.dry_run,
    )
    action = "dry-run" if args.dry_run else "indexed"
    print(
        f"{action}: seen={stats.seen} indexed={stats.indexed} "
        f"skipped={stats.skipped} line_docs={stats.line_docs} errors={stats.errors}"
    )
    return 1 if stats.errors else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(vector_index_main())
