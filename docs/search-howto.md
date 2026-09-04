# OpenSearch Search & Query How-To

This guide covers how to perform keyword, semantic vector (kNN), and sentiment/mood searches against the Karaoke platform's OpenSearch indices (`tracks` and `lyric_lines`).

## Prerequisites

OpenSearch runs locally (e.g. via Kind cluster or standalone container) at `http://localhost:9200`.

Check cluster health:
```bash
curl -s http://localhost:9200/_cluster/health?pretty
```

---

## 1. Full-Text Keyword Search (BM25)

Use full-text queries when searching for exact song titles, artist names, or explicit words in lyrics.

```bash
curl -X POST "http://localhost:9200/tracks/_search?pretty" \
  -H "Content-Type: application/json" \
  -d '{
    "size": 5,
    "_source": ["title", "artist", "plain_lyrics"],
    "query": {
      "multi_match": {
        "query": "love romance",
        "fields": ["title^2", "plain_lyrics"]
      }
    }
  }'
```

---

## 2. Semantic kNN Vector Search

Find tracks or lines that match conceptually (semantic similarity) even if the exact keywords do not appear in the text. Powered by `lyrics_vector` (384-dimensional embeddings via `all-MiniLM-L6-v2`).

```bash
curl -X POST "http://localhost:9200/tracks/_search?pretty" \
  -H "Content-Type: application/json" \
  -d '{
    "size": 5,
    "_source": ["title", "artist"],
    "query": {
      "knn": {
        "lyrics_vector": {
          "vector": [0.012, -0.043, "... (384 float values)"],
          "k": 5
        }
      }
    }
  }'
```

---

## 3. Sentiment & Mood Queries

Query lyric lines by sentiment score, mood tags, or thematic keywords combined with filters (e.g., release year or artist).

```bash
curl -X POST "http://localhost:9200/lyric_lines/_search?pretty" \
  -H "Content-Type: application/json" \
  -d '{
    "size": 10,
    "_source": ["title", "artist", "text", "mood", "sentiment_score"],
    "query": {
      "bool": {
        "must": [
          {
            "match": {
              "text": "heartbroken memories goodbye"
            }
          }
        ],
        "filter": [
          {
            "term": {
              "mood.keyword": "melancholic"
            }
          }
        ]
      }
    }
  }'
```

---

## 4. Re-indexing and Synchronizing from SQLite

OpenSearch indices are derived from SQLite (`~/.local/share/karaoke/karaoke.db`). You can dry-run or rebuild the index using the indexing scripts or Makefile targets:

```bash
# Dry-run track indexing
karaoke-vector-index --dry-run --no-embed

# Full rebuild including vector embeddings and lyric lines
karaoke-vector-index --rebuild --lines

# Using Makefile
make vector-index-dry-run
make vector-index LINES=1
```
