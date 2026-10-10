# Mood Slider, CDP Window Control, and Analysis Versioning

## 1. Direct Video Playback vs Search Query Fallback
- **Problem:** Browsing/list playback previously converted direct video URLs (`https://www.youtube.com/watch?v=VID`) into YouTube Music search query links (`https://music.youtube.com/search?q=artist+title`). This landed the browser on a search results list requiring an extra manual click to start music.
- **Solution:** `player_open.py` preserves exact video IDs (`v=VID`) and navigates directly to `https://music.youtube.com/watch?v=VID` in YouTube Music so playback starts immediately. Search queries (`/search?q=...`) are used only when no direct video ID exists for a track.
- **Source Priority in `list_tracks`:** In `api.py`, `LEFT JOIN sources` uses explicit priority ordering (`youtube_music > youtube > http > spotify`) so API listing endpoints always return a direct, browser-openable YouTube URL when available.

## 2. Interactive Mood Slider & Real-Time Filtering in Browse Mode
- `browse.py` (`KaraokeBrowser`) features a real-time Energy/Mood slider:
  - Keybindings: `[` / `]` adjust Energy threshold ($0\% \dots 100\%$), `m` cycles presets (`All Moods`, `Chill / Mellow` 🌙, `Groovy / Upbeat` ⚡, `High Energy / Anthem` 🔥).
  - DataTable displays `Artist`, `Title`, `Key`, `BPM`, and `Energy` status (e.g. `🔥 85%`, `⚡ 52%`, `🌙 20%`).
  - Filtering operates in memory ($<1\text{ms}$) on pre-cached track energy data.

## 3. Chrome CDP Window Control & WebTUI Startup Check
- **CDP Window Control (`player_open.py`, `ctrl_api.py`, `api_client.py`)**:
  - `Browser.getWindowForTarget` reads Chrome window ID, position `(left, top, width, height)`, and state (`normal`, `minimized`, `maximized`, `fullscreen`).
  - `Browser.setWindowBounds` sets window geometry or state over CDP.
  - `Page.bringToFront` focuses the Chrome page tab.
  - Control API exposes `GET /api/players/window` and `POST /api/players/window` (states: `"fullscreen"`, `"normal"`, `"minimized"`, `"maximized"`, `"focus"`).
- **WebTUI CDP Probe (`scripts/web_serve.py`)**:
  - Probes `http://localhost:9222/json` before launching `textual-serve`.
  - Outputs `[✓] kiosk-chrome (opt) CDP :9222 active` or `[!] kiosk-chrome (opt) kiosk Chrome CDP :9222 down (unified player off)`.
  - Supports `--start-kiosk` or `KARAOKE_AUTO_KIOSK=1` to auto-launch Chrome kiosk if down.

## 4. Track Analysis Versioning & History
- SQLite `track_analysis_history` table logs immutable historical analysis entries for every scan, sample (`k`), recording decompile, postprocess worker, or CLI run.
- `save_detected(...)` accepts `source_kind` (`sample`, `youtube_cache`, `recording`, `local`, `postprocess`, `cli`).
- Library API exposes `GET /api/tracks/{id}/analysis/history` and `ApiClient.get_track_analysis_history(track_id)`.
