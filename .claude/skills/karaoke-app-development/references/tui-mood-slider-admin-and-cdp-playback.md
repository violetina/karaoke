# TUI Mood Slider, Admin Operations, CDP Playhead Sync, & Analysis Versioning

This reference documents the patterns, API endpoints, and pitfalls established for the TUI mood/energy slider, backend operations dashboard, Chrome CDP playhead sync, and track analysis history.

---

## 1. Track Analysis Versioning & History (`track_analysis_history`)

When tracks are sampled, scanned, or re-analysed across multiple sources (e.g. sample mode vs full YouTube audio vs recording vs local file), the canonical row stays in `track_analysis`, while every detection event is logged to `track_analysis_history`:

- **Schema:** `track_analysis_history(history_id AUTOINCREMENT, track_id, source_kind, detected_key, key_confidence, key_agreement, bpm, method, energy, brightness, created_at)`.
- **`source_kind` values:** `sample`, `recording`, `youtube_cache`, `local`, `postprocess`, `cli`.
- **API Endpoint:** `GET /api/tracks/{track_id}/analysis/history`
- **ApiClient Method:** `get_track_analysis_history(track_id)`

---

## 2. TUI Mood Slider, Sort Controls, & Feeling Glyphs

### A. Feeling Glyph (`feeling_glyph`)
Combines `energy` (arousal) and `brightness` (valence proxy) into an overall track "feeling" glyph:
```python
def feeling_glyph(energy: float, brightness: float | None = None) -> str:
    b = 0.5 if brightness is None else brightness
    if energy >= 0.66:
        return "🔥" if b < 0.5 else "☀"
    if energy >= 0.4:
        return "⚡" if b >= 0.5 else "🌗"
    return "♡" if b >= 0.5 else "🌙"
```

### B. Interactive Mood Slider & Sort Controls
- **Mood Slider (`-` / `+` keys):** Adjusts energy floor (`_mood_level` $0.0 \dots 1.0$). Filters both library DataTable and search results in real time.
- **Preset Selector (`_mood_filter`):** Quick jumps (`All`, `High Energy >75%`, `Groovy 40-75%`, `Chill <40%`).
- **Sort Selector (`_sort`):** `Artist A-Z`, `Energy high->low`, `Energy low->high`, `BPM fast->slow`, `BPM slow->fast`, `Key`.

---

## 3. TUI Playlist & Queue Controls

- **`a` (Enqueue Track):** Appends selected track from search or library to the end of `self._queue` without interrupting playback.
- **`U` (Shuffle Queue):** Randomly shuffles unplayed upcoming items (`self._queue[self._queue_at + 1:]`).
- **`C` (Clear Queue):** Clears `self._queue` and resets `_queue_at = -1`.
- **`o` (Toggle Play-Once / Auto-Advance):** `_watch_queue()` checks `browser_playback()` for track completion and auto-advances `action_queue_next()`.

---

## 4. Chrome CDP Playhead Sync & Kiosk Self-Healing

### A. Live Video Playhead in `_tick_lyrics()`
- **Problem:** `playerctl.position()` on Chrome/YouTube Music MPRIS often returns static/frozen values (`0.5s`), causing lyrics subtitles to freeze at `next in 0.5s`.
- **Fix:** In `_tick_lyrics()`, query `browser_playback()` (`<video>.currentTime` via Chrome CDP port `:9222`) FIRST. Live `<video>.currentTime` updates continuously every 0.2s, ensuring smooth lyric scrolling and countdowns. Fall back to `playerctl.position()` only when CDP is inactive.

### B. Self-Healing Kiosk Browser (`launch_kiosk_browser`)
- When selecting/playing a track, `open_song_url()` tries `try_chrome_cdp_navigate(url)`. If CDP is down, `launch_kiosk_browser(url)` automatically spawns `google-chrome-stable --app=... --remote-debugging-port=9222 --user-data-dir=~/.local/share/karaoke/kiosk-chrome &`, waits 1.2s, and navigates in-window — avoiding external browser tab clutter.

### C. Direct Watch Link Preservation
- Direct video URLs (`https://music.youtube.com/watch?v=VID` or `www.youtube.com/watch?v=VID`) are preserved rather than converted to search queries (`/search?q=...`), ensuring songs start playing immediately upon selection.

---

## 5. Backend Operations & Admin TUI (`make admin`)

- **Admin Dashboard (`src/karaoke/admin_tui.py` / `make admin`):**
  - **Worker Pool Management:** Displays systemd `karaoke-postprocess@1..6.service` states, CPU%, Memory (MB), Queue Depth. Keys `[+]` Scale up, `[-]` Scale down, `[R]` Restart worker pool.
  - **Error Diagnostics:** Real-time error log tail viewer (`GET /api/logs/errors` / `karaoke.log`).
  - **File Ingestion:** Ingest folder / audio file paths directly (`POST /api/scan/folder`).
- **Control API Endpoints (`src/karaoke/ctrl_api.py`):**
  - `GET /api/workers/status`
  - `POST /api/workers/scale` (`target: 0..6`)
  - `GET /api/logs/errors` (`lines: 50..500`)

---

## 6. Sourceless Track Auto-Resolution

To resolve tracks in SQLite (`tracks`) that have 0 sources in `sources`:
```python
hits = youtube.search(f"{artist} - {title}", limit=1)
if hits and hits[0].get("url"):
    conn.execute("INSERT INTO sources (track_id, kind, url) VALUES (?, ?, ?)", (tid, "youtube", hits[0]["url"]))
```
Fallback for duplicate URLs: `https://www.youtube.com/results?search_query=...` with `kind="youtube_search"`.
