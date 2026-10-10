# Recording E2E Testing, Folder Scanner Fallbacks & Analysis Versioning

This reference details the patterns for recording lifecycle E2E testing, folder scanner extensions, and track analysis versioning.

## 1. Recording Processing API Lifecycle & E2E Testing

Recording management and processing spans both the Library API (`karaoke.api` at `:8000`) and Control API (`karaoke.ctrl_api` at `:8765`):

- **Listing & Filtering:** `GET /api/recordings?q=...&status=...&has_marks=true&keep_audio=true`
- **Detail Inspection:** `GET /api/recordings/{id}` returns captured duration, file size, and grouped track slices (`recording_slice.segments(marks)`).
- **Metadata Update:** `PATCH /api/recordings/{id}` updates `note` and `keep_audio`.
- **Live Capture Control:** `POST /api/record/start`, `GET /api/record/status`, `POST /api/record/stop`. Re-attaches automatically if a capture session is already active for the source.
- **Decompiling & Discarding:** `POST /api/recordings/{id}/analyse` decompiles the segment into DB track analysis and OpenSearch audio vectors; `DELETE /api/recordings/{id}/audio` frees FLAC audio files while preserving markers.
- **E2E Test Pattern:** See `tests/test_e2e_recordings_api.py` and `scripts/e2e_test_recordings_api.py`. When testing `ApiClient` against FastAPI `TestClient`, clean query parameters using `ApiClient._clean_params` to avoid sending explicit `None` query strings (`?status=None`).

## 2. Folder Scanner Engine Fallbacks (`karaoke.folder_scan`)

- **Format Support:** `.webm` audio files must be included in `tags.AUDIO_EXTS` to recognize yt-dlp YouTube cache files.
- **Filename Video ID Extraction:** `localcache.extract_youtube_id` strips extensions/paths so filenames like `/path/-1jPUB7gRyg.webm` resolve to video ID `-1jPUB7gRyg`.
- **YouTube Cache Video ID Fallback:** If ID3 tags are missing/empty, `folder_scan` looks up `sources.url LIKE '%vid%'` in SQLite to resolve `(artist, title)` instantly without network calls.
- **Recording Session Marker Fallback:** For `seg-*.flac` files in `recordings/`, `folder_scan` checks `recordings` + `recording_marks` tables to resolve track metadata.

## 3. Track Analysis Versioning (`track_analysis_history`)

- **Schema:** `track_analysis_history` stores an immutable log of all analysis runs (scans, sample mode `k`, postprocessing workers, CLI).
- **Source Kind Tagging:** `save_detected(..., source_kind=...)` tags each entry as `sample`, `youtube_cache`, `recording`, `local`, `postprocess`, or `cli`.
- **API Access:** `GET /api/tracks/{id}/analysis/history` and `ApiClient.get_track_analysis_history(track_id)` expose the full version log for comparing sample vs full scan, or local vs streaming audio variants.

## 4. Vector Search & Hybrid kNN Patterns (OpenSearch)

- **`tracks` (`lyrics_vector`):** 384-d dense embedding from `all-MiniLM-L6-v2` for high-level semantic search.
- **`tracks-lines` (`line_vector`):** 384-d dense embedding on individual lyric lines for precise concept search.
- **`karaoke-clap` (`clap_vector`):** 512-d zero-shot audio embedding from `laion/clap-htsat-fused` for acoustic/texture/instrument queries.
- **Hybrid Filtering:** Use `filter` inside the `knn` query block (e.g. `must_not` term filters for excluded artists) to combine vector ranking with metadata constraints.
