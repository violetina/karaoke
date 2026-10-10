# Folder Scanning, Play Sessions & SSE Sampling

## 1. Music Folder Scanning (`karaoke.folder_scan`)
The folder scanner (`scan_and_ingest_folder`) ingests local audio collections (`~/Music`, `~/.local/share/karaoke/youtube`, `~/.local/share/karaoke/recordings`) into SQLite (`tracks`, `sources`, `lyrics`, `track_analysis`, `track_genre`).

### Key Resolution Hierarchy
1. **Mutagen Tags**: Extract normalized artist, title, album, year, duration via `tags.extract_tags`.
2. **YouTube Video ID Fallback**: Extract 11-char video ID from filename/stem (e.g. `-1jPUB7gRyg.webm`) and query SQLite `sources`/`tracks` — resolves cached YouTube downloads instantly with 0 network calls.
3. **Recording Session Markers Fallback**: For segment files in `recordings/` (`seg-*.flac`), match `rec_dir` in `recordings` table and use `recording_marks` to resolve `(artist, title)`.
4. **Shazam / songrec Fingerprinting**: For untagged/unknown local tracks, run `identify_file_fingerprint(path)` (ffmpeg 10s slice -> `songrec`).

### Streaming Resolution Optimization
When `resolve_streaming=True`, if `vid` is already extracted from the path or filename, set `yt_url = f"https://www.youtube.com/watch?v={vid}"` directly rather than invoking `youtube.search()`.

---

## 2. Audio-First YouTube Music Playback
When opening YouTube/YT Music links from browse/list mode with known artist and title:
- **Rule**: Pass `prefer_audio=True` and `artist`/`title` to `open_song_url` / `ApiClient.play(...)`.
- **Target URL**: Opens `https://music.youtube.com/search?q=artist+title`.
- **Why**: Direct `watch?v=...` links can land on YouTube Music's video mode, whose video clip is frequently out of sync with the audio track. Search lets YouTube Music select the canonical album audio.

---

## 3. Play Sessions & SSE Sample Streaming
- **Play Sessions**: `POST /api/play` assigns a unique `session_id` (`play_...`) and tracks launched sessions in `_PLAY_SESSIONS`.
  - `GET /api/play/sessions`: list active/recent sessions.
  - `GET /api/play/sessions/{session_id}`: inspect session.
  - `DELETE /api/play/sessions/{session_id}`: pause playerctl and mark session stopped.
- **SSE Sample Progress**: `GET /api/sample/stream?artist=A&title=B&seconds=45` streams real-time Server-Sent Events (`start`, `progress`, `complete`, `error`) so TUIs and Web UIs get live capture progress without direct in-process callbacks.
- **ApiClient Helper**: `ApiClient.sample_stream_url(...)` returns the formatted EventSource URL.

---

## 4. RabbitMQ Reconnect & Templated systemd Workers
- **Worker Reconnect**: `postprocess_worker.py` wraps `_consume_once` in a `while True` loop with exponential backoff (`delay = min(60.0, delay * 1.7)`). Routine broker or port-forward drops reconnect automatically without crashing the systemd service.
- **Templated Systemd Units**: `karaoke.target` launches `karaoke-postprocess@1.service` through `karaoke-postprocess@6.service`. Every instance competes on the same queue (`prefetch_count=1`), capped together under `karaoke-postprocess.slice` (`CPUQuota=600%`).
