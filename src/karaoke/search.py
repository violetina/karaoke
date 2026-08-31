"""Semantic + keyword search over the tracks index."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .config import settings


@dataclass
class SearchHit:
    score: float
    artist: str
    title: str
    album: str
    source: str
    has_synced: bool
    path: Optional[str] = None


def _hit(raw: dict[str, Any]) -> SearchHit:
    s = raw["_source"]
    return SearchHit(
        score=raw.get("_score", 0.0),
        artist=s.get("artist", ""),
        title=s.get("title", ""),
        album=s.get("album", ""),
        source=s.get("source", ""),
        has_synced=bool(s.get("has_synced")),
        path=s.get("path"),
    )


def semantic_search(query: str, k: int = 5, os_client: Any = None) -> list[SearchHit]:
    """kNN search over lyric vectors: 'find the song that goes like ...'."""
    from .embed import embed_text
    from .osclient import client

    c = os_client or client()
    vec = embed_text(query)
    body = {"size": k, "query": {"knn": {"lyrics_vector": {"vector": vec, "k": k}}}}
    res = c.search(index=settings.index_name, body=body)
    return [_hit(h) for h in res["hits"]["hits"]]


def keyword_search(query: str, k: int = 5, os_client: Any = None) -> list[SearchHit]:
    """Full-text search across title/artist/album/plain lyrics."""
    from .osclient import client

    c = os_client or client()
    body = {
        "size": k,
        "query": {
            "multi_match": {
                "query": query,
                "fields": ["title^2", "artist^2", "album", "plain_lyrics"],
            }
        },
    }
    res = c.search(index=settings.index_name, body=body)
    return [_hit(h) for h in res["hits"]["hits"]]


def find_track(artist: str, title: str, os_client: Any = None) -> Optional[dict[str, Any]]:
    """Look up a specific indexed track by EXACT artist+title (cache lookup).

    Uses case-insensitive exact matching on both fields so a fuzzy title
    collision can't return the wrong track's cached lyrics. Falls back to a
    title-only exact match when no artist is supplied.
    """
    from .osclient import client

    c = os_client or client()
    must: list[dict[str, Any]] = [
        {"match_phrase": {"title": title}},
    ]
    if artist:
        must.append({"match_phrase": {"artist": artist}})
    body = {"size": 1, "query": {"bool": {"must": must}}}
    res = c.search(index=settings.index_name, body=body)
    hits = res["hits"]["hits"]
    if not hits:
        return None
    src = hits[0]["_source"]
    # Guard: confirm the returned title actually matches (case-insensitive).
    if src.get("title", "").strip().lower() != title.strip().lower():
        return None
    if artist and src.get("artist", "").strip().lower() != artist.strip().lower():
        return None
    return src
