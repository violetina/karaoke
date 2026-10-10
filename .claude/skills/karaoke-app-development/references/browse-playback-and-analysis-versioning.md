# Browse Playback, Folder Scanning & Analysis Versioning Lessons

## 1. Direct Playback vs Search Fallback (`player_open.py` & `api.py`)
- **Direct Watch Link Preservation**: When launching playback for a track with a known video ID (`v=VID`), `open_song_url` must preserve the direct watch link (`https://music.youtube.com/watch?v=VID`) rather than converting it into a search query (`https://music.youtube.com/search?q=Artist+Title`).
  - Search queries land on a YouTube Music **search results list** where nothing plays automatically until clicked.
  - Direct watch links load the exact video/audio track and **start playing immediately**.
- **Source Priority in `api.py` (`list_tracks`)**: SQL `LEFT JOIN sources` must rank sources by priority (`youtube_music > youtube > http > spotify`) so that tracks with multiple source rows (e.g. Spotify metadata + YouTube video) return the direct playable YouTube URL to `ApiClient` and browse mode.

## 2. YouTube Cache & Recording Session Folder Scanning (`folder_scan.py`, `tags.py`, `localcache.py`)
- **`.webm` Audio Extension**: YouTube cache files downloaded as `.webm` must be included in `tags.AUDIO_EXTS` so `iter_audio_files` recognizes them during folder scans.
- **Filename YouTube ID Extraction**: `localcache.extract_youtube_id` strips extensions/paths so filenames like `-1jPUB7gRyg.webm` resolve to the 11-char video ID `-1jPUB7gRyg`.
- **Instant YouTube Cache Ingestion**: `folder_scan.py` checks SQLite `sources` for matching video IDs first, instantly populating `(artist, title)` without network `youtube.search` calls.
- **Recording Session Marker Resolution**: Audio segments (`seg-*.flac` in `recordings/`) resolve their track metadata via SQLite `recordings` + `recording_marks`.

## 3. Analysis Versioning (`track_analysis.py`)
- **`track_analysis_history`**: Every key/BPM analysis write updates canonical `track_analysis` and appends an immutable entry to `track_analysis_history` with `source_kind` (`sample`, `youtube_cache`, `recording`, `local`, `postprocess`, `cli`).
- Exposed via `GET /api/tracks/{id}/analysis/history` and `ApiClient.get_track_analysis_history(track_id)`.

## 4. WebTUI & CDP Window Controls (`web_serve.py`, `player_open.py`, `ctrl_api.py`)
- **CDP :9222 Status Line**: `scripts/web_serve.py` probes `http://localhost:9222/json` on startup/restart:
  - `[✓] kiosk-chrome   (opt)  CDP :9222 active`
  - `[!] kiosk-chrome   (opt)  kiosk Chrome CDP :9222 down (unified player off)`
- **CDP Window Control**: `Browser.getWindowForTarget` and `Browser.setWindowBounds` exposed over `GET/POST /api/players/window` and `ApiClient` to inspect and control Chrome window state (`normal`, `fullscreen`, `minimized`, `maximized`, `focus`) and geometry.
