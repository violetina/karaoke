"""Semantic + keyword search over the tracks index."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .config import settings
from .librarysearch import TRANSCRIBED_SOURCE, is_transcribed

# How much a match is worth when the "lyrics" are Whisper's own guess at the
# words. Shared rule with librarysearch so the two search paths cannot drift
# into disagreeing about which tracks are trustworthy.
TRANSCRIBED_BOOST = 0.25


@dataclass
class SearchHit:
    """One normalized search result from OpenSearch."""

    score: float
    artist: str
    title: str
    album: str
    source: str
    has_synced: bool
    path: Optional[str] = None
    # Whether the words are Whisper's guess rather than a real lyric. Carried
    # on the hit so a caller can say so, instead of every caller having to know
    # which source strings mean "transcribed".
    transcribed: bool = False


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
        transcribed=is_transcribed(s.get("lyrics_source", "")),
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
            "boosting": {
                "positive": {
                    "multi_match": {
                        "query": query,
                        "fields": ["title^2", "artist^2", "album", "plain_lyrics"],
                    }
                },
                # Demoted, not filtered. A transcription is the only text 69
                # tracks have, so removing them would make those songs
                # unfindable by their words; they simply must not outrank a
                # track whose lyrics are real. `whisper_aligned` is untouched:
                # there the words came from a real source and only the timings
                # are Whisper's.
                "negative": {"term": {"lyrics_source": TRANSCRIBED_SOURCE}},
                "negative_boost": TRANSCRIBED_BOOST,
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
