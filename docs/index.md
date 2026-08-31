# Karaoke

Karaoke is a local terminal sing-along and lyric search platform.

It combines local music metadata, LRCLIB synced lyrics, optional Whisper transcription, Spotify playback position sync, live song identification through songrec, and an OpenSearch cache/index running on a local kind cluster.

## What it provides

- `karaoke`: render time-synced lyrics in the terminal.
- `lyricsearch`: search indexed lyrics semantically or by keyword.
- `music-index`: scan local audio files into OpenSearch.
- `karaoke-stats`: play counts and radio-discovery stats.
- OpenSearch cache: stores metadata, plain lyrics, synced LRC lyrics and semantic vectors.
- Local SQLite cache: offline lyrics for known songs + play/discovery stats (no cluster needed).
- Live sync modes: Spotify position, microphone/room audio, laptop output monitor and continuous radio mode.

## Documentation map

- [Architecture](architecture.md): components, runtime modes and data flows.
- [Cache schema](cache-schema.md): the OpenSearch `tracks` index and cache/id conventions.
- [Local cache and stats](local-cache-and-stats.md): offline lyrics cache and `karaoke-stats`.
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
