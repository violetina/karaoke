# Karaoke

Karaoke is a local terminal sing-along and lyric search platform.

It combines local music metadata, LRCLIB synced lyrics, optional Whisper transcription, Spotify playback position sync, live song identification through songrec, a SQLite operational database, and optional OpenSearch vector indexes for semantic search/training experiments.

## What it provides

- `karaoke`: render time-synced lyrics in the terminal.
- SQLite database: source of truth for known tracks, source URLs/URIs, lyrics, play/radio stats and backfill gaps.
- `lyricsearch`: semantic "find the song that goes '...'" via lyric embeddings (OpenSearch-derived index).
- `music-index`: scan local audio files into OpenSearch when vector search/training indexes are wanted.
- OpenSearch vector indexes: optional derived metadata/lyrics/line vectors for semantic search, sentiment and timing-training experiments.
- Local SQLite cache: offline lyrics for known songs + play/discovery stats (no cluster needed).
- Live sync modes: Spotify position, microphone/room audio, laptop output monitor and continuous radio mode.

## Documentation map

- [Architecture](architecture.md): components, runtime modes and data flows.
- [Cache schema](cache-schema.md): the optional OpenSearch vector index and cache/id conventions.
- [Local cache and stats](local-cache-and-stats.md): SQLite source-of-truth schema, offline lyrics cache and `karaoke-stats`.
- [Vector search and training plan](vector-search-plan.md): rebuild OpenSearch from SQLite for semantic search, sentiment and timing experiments.
- [API reference](api.md): generated from Python docstrings via mkdocstrings.
- [Workflow](workflow.md): operational project flow.
- [Troubleshooting](troubleshooting.md): known runtime and setup issues.

## Quick start

```bash
python3.14 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
make test
```

Build the docs locally:

```bash
make docs
```
