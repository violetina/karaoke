# karaoke-000

A local karaoke + lyric-search platform.

- **Scan** a music library (`~/Music`) and/or import your **Spotify** library as metadata.
- **Index** track metadata + lyrics into **OpenSearch** (with kNN vector search) on a local **kind** cluster.
- **Fetch** synced (timestamped) lyrics from **LRCLIB** (free, no key), cached in OpenSearch.
- **Karaoke** CLI: identify the current song (file / text / live via `songrec`), then render time-synced highlighted lyrics.
- **lyricsearch**: semantic "find the song that goes '...'" via lyric embeddings (sentence-transformers).
- **Whisper fallback**: for local files with no LRCLIB match, transcribe to approximate synced lyrics with faster-whisper.

## Source model (hybrid)

- `~/Music` = audio + cache source. Karaoke playback and Whisper transcription run here.
- Spotify (your account) = optional metadata seed (liked songs / playlists). Spotify's API does **not** allow audio download, so no Whisper for Spotify-only tracks; lyrics come from LRCLIB.
- All lyrics cached in OpenSearch → repeat plays are offline.

## Layout

```
src/karaoke-000/
├── karaoke/        # the package
├── tests/          # pytest
deploy/             # kind + opensearch + scanner Job manifests
```

## Safety

The scanner/deploy scripts **only** talk to the `kind-karaoke` kube-context. They abort if the current
context is not `kind-*`, so nothing can touch the AKS work clusters.

See the implementation plan: `~/.hermes/plans/2026-08-29_173500-local-karaoke-platform.md`.

## Dev quickstart

```bash
python3.14 -m venv .venv && . .venv/bin/activate
pip install -e "src/karaoke-000[dev]"
pytest src/karaoke-000/tests -v
```
