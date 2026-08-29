"""OpenSearch client + index bootstrap for karaoke-000."""
from __future__ import annotations

from typing import Any

from opensearchpy import OpenSearch

from .config import settings


def client() -> OpenSearch:
    """Return an OpenSearch client for the configured endpoint."""
    return OpenSearch(
        hosts=[settings.opensearch_url],
        http_compress=True,
        use_ssl=settings.opensearch_url.startswith("https"),
        verify_certs=False,
        ssl_show_warn=False,
        timeout=30,
    )


def index_body() -> dict[str, Any]:
    """Mapping for the `tracks` index: metadata + lyrics + kNN lyric vector."""
    return {
        "settings": {"index": {"knn": True, "number_of_replicas": 0}},
        "mappings": {
            "properties": {
                "path": {"type": "keyword"},
                "title": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
                "artist": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
                "album": {"type": "text"},
                "year": {"type": "integer"},
                "duration": {"type": "float"},
                "source": {"type": "keyword"},          # local | spotify
                "has_synced": {"type": "boolean"},
                "lyrics_source": {"type": "keyword"},    # lrclib | whisper | none
                "plain_lyrics": {"type": "text"},
                "synced_lyrics": {"type": "text", "index": False},  # raw LRC, retrieved not searched
                "lyrics_vector": {
                    "type": "knn_vector",
                    "dimension": settings.embed_dim,
                    "method": {
                        "name": "hnsw",
                        "space_type": "cosinesimil",
                        "engine": "lucene",
                    },
                },
                "indexed_at": {"type": "date"},
            }
        },
    }


def ensure_index(os_client: OpenSearch | None = None) -> bool:
    """Create the tracks index if absent. Returns True if it created it."""
    c = os_client or client()
    if c.indices.exists(index=settings.index_name):
        return False
    c.indices.create(index=settings.index_name, body=index_body())
    return True


def _cli() -> int:  # pragma: no cover - manual bootstrap
    import argparse

    ap = argparse.ArgumentParser(description="OpenSearch index bootstrap")
    ap.add_argument("--ensure-index", action="store_true")
    ap.add_argument("--info", action="store_true")
    a = ap.parse_args()
    c = client()
    if a.ensure_index:
        created = ensure_index(c)
        print(f"index '{settings.index_name}': {'created' if created else 'already exists'}")
    if a.info or not a.ensure_index:
        health = c.cluster.health()
        print(f"cluster={health.get('cluster_name')} status={health.get('status')}")
        if c.indices.exists(index=settings.index_name):
            count = c.count(index=settings.index_name).get("count", 0)
            print(f"index '{settings.index_name}' docs={count}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
