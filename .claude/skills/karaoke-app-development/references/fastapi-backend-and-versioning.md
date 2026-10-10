# FastAPI Backend Service & Versioning Lessons

## `get_synced` Signature Contract
`get_synced(ref: SongRef, *, use_cache: bool = True, transcribe: bool = False, force_transcribe: bool = False, lyrics_file: Optional[str] = None, stats_mode: Optional[str] = None) -> Lyrics`

- **First positional arg MUST be a `SongRef` object**, not string `artist` and `title`.
- When following live playback (e.g. Spotify `Playback` objects or live recognition results), wrap the metadata in `SongRef` before calling `get_synced`:
  ```python
  ref = SongRef(
      artist=pb.artist,
      title=pb.title,
      duration=pb.duration_ms / 1000.0 if pb.duration_ms else None,
      source="spotify",
  )
  ly = get_synced(ref, use_cache=use_cache, stats_mode="spotify")
  ```

## FastAPI Backend Service Architecture (`src/karaoke/api.py` & `src/karaoke/staging_api.py`)
- Exposes REST endpoints for the song library and player:
  - `GET /health` / `GET /api/health` — Health check
  - `GET /api/tracks?q=...` — List tracks with search filtering
  - `GET /api/tracks/{track_id}` — Details, source URLs, approved lyrics
  - `POST /api/play` — Open/launch media via `open_song_url()` (on the control API)
  - `GET /api/stats` — Local cache summary metrics
  - `GET /api/logs?lines=100` — Recent application log lines
- **Staging Supervisor Endpoints** (`src/karaoke/staging_api.py`):
  - `POST /api/staging/youtube` — Dispatches a YouTube caption-staging job to FastAPI's background task runner.
  - `POST /api/staging/whisper` — Dispatches a local file transcription job (using Whisper) to FastAPI's background task runner.
  - Using `BackgroundTasks` allows these expensive download, extraction, and alignment operations to execute asynchronously without blocking the REST request.
- Entrypoints: `karaoke-api` CLI script and `make api` target (`python -m karaoke.api`).
- Playback entrypoints: `karaoke-ctrl-api` CLI script and `make ctrl-api` target (`python -m karaoke.ctrl_api`).

## Version Bump & Editable Install Cleanup
When bumping version in `pyproject.toml` (e.g., `0.1.0` -> `0.1.1`):
1. Update `pyproject.toml`, `src/karaoke/__init__.py`, and `src/karaoke/api.py`.
2. Stale `karaoke-<old-version>.dist-info` and `__editable__.karaoke-<old-version>.pth` in `.venv/lib/python3.1x/site-packages/` can mask the new version or confuse `pip`.
3. Clean build artifacts and stale site-packages metadata:
   ```bash
   make clean
   rm -rf .venv/lib/python3.*/site-packages/karaoke-*.dist-info
   rm -f .venv/lib/python3.*/site-packages/__editable__.karaoke-*.pth
   pip install -e .
   ```
