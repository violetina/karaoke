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


# What cosine similarity between *unrelated* tracks actually looks like,
# measured over 3000 random pairs of library vectors:
#
#     min 0.777   p5 0.885   median 0.965   p95 0.986   max 0.999
#
# Every vector sits in a narrow cone, so a raw score reads far more confident
# than it is: 0.99 is not "nearly identical", it is the top few percent. These
# thresholds exist so a caller can say that rather than print a number that
# invites the wrong conclusion. Re-measure if the vector composition changes.
SIMILARITY_TYPICAL = 0.965      # median between unrelated tracks
SIMILARITY_NOTABLE = 0.986      # p95: closer than 19 of 20 random pairs
SIMILARITY_STRIKING = 0.993     # rarer still


def describe_similarity(score: float) -> str:
    """Plain words for a cosine, given how compressed the range is."""
    if score >= SIMILARITY_STRIKING:
        return "very close"
    if score >= SIMILARITY_NOTABLE:
        return "close"
    if score >= SIMILARITY_TYPICAL:
        return "somewhat"
    return "unremarkable"


@dataclass
class SoundHit:
    """One "sounds like" result: a track, and how close it sounded."""

    track_id: int
    artist: str
    title: str
    similarity: float          # cosine, 0..1 -- the vectors are unit length
    source: str                # library (the release) or recording (a capture)
    detected_key: str = ""
    bpm: Optional[float] = None


def audio_vector_for(track_id: int, os_client: Any = None) -> Optional[list[float]]:
    """A track's stored sound vector, preferring the release over a capture.

    A track can have both: a recording describes one performance heard through
    a speaker and taken off a monitor, while the library document is the
    release itself. The release is the cleaner query -- a capture carries the
    room, the speaker and the codec of that particular evening.
    """
    from . import audio_vector
    from .osclient import client

    c = os_client or client()
    try:
        res = c.search(index=audio_vector.AUDIO_INDEX, body={
            "size": 1,
            "query": {"bool": {
                "must": [{"term": {"track_id": track_id}}],
                "should": [{"term": {"source": "library"}}],
            }},
            "sort": [{"_score": "desc"}],
        })
    except Exception:
        return None
    hits = res["hits"]["hits"]
    return hits[0]["_source"].get("audio_vector") if hits else None


def similar_sounding(track_id: int, k: int = 10,
                     os_client: Any = None) -> list[SoundHit]:
    """Tracks whose audio resembles this one, nearest first.

    This is query-by-example rather than by text, because a 62-dimension timbre
    and harmony vector shares no space with a sentence embedding -- there is no
    sensible way to type a query into it. What it answers is the question the
    lyric index cannot: *what does this sound like*, which for an instrumental
    is the only question there is.

    Results are deduplicated by track: a song present as both a release and a
    capture would otherwise occupy two of the k slots with itself.
    """
    from . import audio_vector
    from .osclient import client

    vector = audio_vector_for(track_id, os_client)
    if vector is None:
        return []

    c = os_client or client()
    try:
        # Over-fetch: the query track's own documents and any duplicate
        # observations come back too, and are dropped below.
        res = c.search(index=audio_vector.AUDIO_INDEX, body={
            "size": max(k * 3, k + 5),
            "query": {"knn": {"audio_vector": {"vector": vector,
                                               "k": max(k * 3, k + 5)}}},
        })
    except Exception:
        return []

    seen: set[int] = {track_id}
    out: list[SoundHit] = []
    for hit in res["hits"]["hits"]:
        src = hit["_source"]
        tid = int(src.get("track_id", 0))
        if tid in seen:
            continue
        seen.add(tid)
        out.append(SoundHit(
            track_id=tid,
            artist=src.get("artist", ""),
            title=src.get("title", ""),
            # OpenSearch returns 1/(1+distance) for cosine; the stored vectors
            # are unit length, so reporting the raw score would be a different
            # number from the cosine everything else in this project uses.
            similarity=audio_vector.similarity(vector, src["audio_vector"])
            if src.get("audio_vector") else 0.0,
            source=src.get("source", ""),
            detected_key=src.get("detected_key", "") or "",
            bpm=src.get("bpm"),
        ))
        if len(out) >= k:
            break
    return out


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


def similar_main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    """CLI: tracks that sound like a given one.

    Query by example, because a timbre vector shares no space with text --
    there is nothing sensible to type into it. The reported closeness is
    described in words as well as numbers, since the raw cosine is compressed
    into a narrow band and a bare "0.99" invites the wrong conclusion.
    """
    import argparse

    from . import localcache

    ap = argparse.ArgumentParser(
        prog="karaoke-similar",
        description="Find tracks whose audio resembles a given track")
    ap.add_argument("query", help="artist and title, or a track id")
    ap.add_argument("-k", type=int, default=8, help="how many to show")
    args = ap.parse_args(argv)

    conn = localcache.connect()
    try:
        if args.query.isdigit():
            track_id = int(args.query)
            row = conn.execute("SELECT artist, title FROM tracks WHERE track_id = ?",
                               (track_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT track_id, artist, title FROM tracks"
                " WHERE (artist || ' ' || title) LIKE ? COLLATE NOCASE"
                " ORDER BY length(artist || title) LIMIT 1",
                (f"%{args.query}%",)).fetchone()
            track_id = int(row["track_id"]) if row else 0
        if row is None:
            print(f"no track matching {args.query!r}")
            return 1
    finally:
        conn.close()

    print(f"sounds like: {row['artist']} - {row['title']}\n")
    hits = similar_sounding(track_id, k=args.k)
    if not hits:
        print("No sound vector for that track.")
        print("Vectors come from record-mode analysis or scripts/vectorize_cached.py,")
        print("so a track with neither has nothing to compare.")
        return 0

    for hit in hits:
        key = hit.detected_key or ""
        bpm = f"{hit.bpm:5.0f}" if hit.bpm else "     "
        print(f"  {hit.similarity:.3f} {describe_similarity(hit.similarity):<13} "
              f"{hit.artist[:22]:24} {hit.title[:26]:28} {key:9} {bpm} "
              f"{hit.source}")

    print(f"\nBetween unrelated tracks the median is {SIMILARITY_TYPICAL:.3f} and "
          f"the 95th percentile {SIMILARITY_NOTABLE:.3f},")
    print("so treat anything below 'close' as ordinary rather than a match.")
    return 0
