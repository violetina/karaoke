# Vector search and training plan

!!! note "This is the original plan, not the current state"

    It describes *proposed* indexes and predates the implementation.
    For what actually exists, how well each index is populated, and
    where the gaps are, see [Vectors](vectors.md).

Karaoke should be driven by the local SQLite database during playback, but it can still use OpenSearch as a rebuildable vector/training index.

## Current direction

- SQLite (`~/.local/share/karaoke/karaoke.db`) is the operational source of truth for:
  - known tracks;
  - source URLs/URIs;
  - approved/staged/rejected lyrics;
  - play/radio history;
  - missing-lyrics gaps for backfill.
- OpenSearch is optional and derived. If it is down, playback should still work.
- The OpenSearch index can be rebuilt from SQLite and local files.

## Why keep OpenSearch

OpenSearch is still useful for capabilities that are heavier than exact playback lookup:

1. Semantic lyric search: “find the song/line that feels like …”.
2. Similar-line lookup for renderer heuristics and training examples.
3. Sentiment/mood search over lines or sections.
4. Timing improvement experiments, e.g. detecting places where a line should pause or break because the audio has an instrumental gap.
5. Future learning loops: compare rendered line timing, manual nudges, gaps, and lyric sections to suggest better sync behavior.

## Proposed derived indexes

### `tracks`

One document per track, derived from SQLite `tracks` + preferred `sources` + approved `lyrics`.

Suggested fields:

- `track_id` integer: SQLite track id.
- `artist`, `title`, `album`, `duration`.
- `source_kind`, `source_url` / `source_uri`, `path`.
- `lyrics_source`.
- `has_plain`, `has_synced`.
- `plain_lyrics`.
- `synced_lyrics` raw LRC, not analyzed for ranking.
- `lyrics_vector`: embedding of full lyrics, or artist/title fallback.
- `indexed_at`.

### `lyric_lines`

One document per lyric line, derived from approved synced lyrics.

Suggested fields:

- `track_id`, `line_index`.
- `artist`, `title`.
- `start_s`, `end_s`, `duration_s`.
- `text`.
- `section_index`, `paragraph_index` when available.
- `mood`, `sentiment_score` from `karaoke.sentiment`.
- `line_vector`: embedding of the line text.
- `context_vector`: embedding of previous/current/next line.

### `timing_training_events`

One document per timing observation, derived from future SQLite training tables or logs.

Suggested fields:

- `track_id`, `line_index`.
- `event_kind`: `manual_nudge`, `long_line`, `instrumental_gap`, `line_break_candidate`, `skip_silence`.
- `position_s`, `delta_s`, `confidence`.
- `audio_features`: optional beat/silence/chroma summary.
- `text`, `context`.
- `event_vector`.

## SQLite additions for training

Keep durable raw observations in SQLite first; OpenSearch indexes them later.

Possible future tables:

```sql
CREATE TABLE IF NOT EXISTS lyric_line_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id      INTEGER NOT NULL,
    line_index    INTEGER NOT NULL,
    position_s    REAL,
    delta_s       REAL NOT NULL,
    reason        TEXT,
    created_at    REAL NOT NULL,
    FOREIGN KEY(track_id) REFERENCES tracks(track_id)
);

CREATE TABLE IF NOT EXISTS lyric_line_features (
    feature_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id      INTEGER NOT NULL,
    line_index    INTEGER NOT NULL,
    start_s       REAL,
    end_s         REAL,
    duration_s    REAL,
    text          TEXT,
    mood          TEXT,
    pause_before_s REAL,
    pause_after_s  REAL,
    created_at    REAL NOT NULL,
    UNIQUE(track_id, line_index),
    FOREIGN KEY(track_id) REFERENCES tracks(track_id)
);
```

## Re-index workflow

Implemented command:

```bash
karaoke-vector-index --dry-run --no-embed --lines
karaoke-vector-index --rebuild
karaoke-vector-index --rebuild --lines
make vector-index-dry-run
make vector-index LINES=1
```

Future flags can add changed-only behavior once SQLite stores per-row `updated_at` timestamps.

Implementation flow:

1. Read tracks, preferred source and approved lyrics from SQLite.
2. Parse LRC with `parse_lrc`.
3. Compute embeddings with the existing `embed` module.
4. Ensure OpenSearch mappings via `osclient.ensure_index`.
5. Bulk upsert `tracks` and `lyric_lines` documents.
6. Never block playback if OpenSearch is unavailable; report indexing failure and leave SQLite untouched.

## Playback rule

Runtime playback should not depend on OpenSearch:

1. exact URL/source lookup in SQLite;
2. exact artist/title lookup in SQLite;
3. LRCLIB / staging / backfill;
4. optional OpenSearch only for search/training features.

This keeps the app fast and offline-friendly while preserving a path for semantic and ML-style features later.
