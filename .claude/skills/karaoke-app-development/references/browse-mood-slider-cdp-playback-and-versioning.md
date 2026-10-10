# Browse Mode, Mood Slider, CDP Playback, and Analysis Versioning

This document summarizes critical patterns, pitfalls, and fixes discovered during the implementation of the API-first browse mode, interactive mood slider, Chrome CDP window control, and track analysis versioning.

---

## 1. Live Chrome CDP Playhead Tracking (`_tick_lyrics`)

- **The Pitfall:** Chromium/Chrome MPRIS implementation on Linux does NOT update `Position` continuously over D-Bus via `playerctl`. `playerctl.position()` returns static snapshots (e.g. `0.5s`), freezing lyrics rendering and locking the border subtitle at `next in 0.5s`.
- **The Solution:** In `_tick_lyrics()`, probe `browser_playback()` (`<video>.currentTime` via Chrome CDP port `:9222`) FIRST before falling back to `playerctl.position()`:
  ```python
  b_state = browser_playback()
  if b_state and b_state.get("present") and b_state.get("position") is not None:
      pos = float(b_state["position"])
  else:
      pos = playerctl.position(self._control_player())
  ```
- **Outcome:** Lyrics highlight, scroll, and countdown subtitles advance continuously in real time.

---

## 2. Self-Healing Kiosk Chrome Launch (`launch_kiosk_browser`)

- **The Pitfall:** When the kiosk Chrome window on port `:9222` is closed, calling `open_song_url` falls back to `xdg-open`, which dumps playback into new tabs in the default browser instead of the dedicated player window.
- **The Solution:** Add `launch_kiosk_browser(url)` to `player_open.py`:
  - Probes `:9222`. If down, launches `google-chrome-stable --app=https://music.youtube.com --remote-debugging-port=9222 --user-data-dir=~/.local/share/karaoke/kiosk-chrome`.
  - Waits 1.2s for socket readiness and retries `try_chrome_cdp_navigate(url)`.
- **Outcome:** Selecting a track automatically self-heals a closed player window instead of spawning browser tabs.

---

## 3. Preserving Direct Video Watch Links for Immediate Playback

- **The Pitfall:** Overwriting direct video watch URLs (`watch?v=VID`) into YouTube Music search queries (`https://music.youtube.com/search?q=Artist+Title`) forces Chrome to open a **search results list**, requiring a manual click to start music.
- **The Solution:** In `player_open.py`, extract 11-char video ID `vid`. If present, convert `www.youtube.com/watch?v=VID` $\rightarrow$ `music.youtube.com/watch?v=VID`.
- **Outcome:** Selecting a track in browse mode opens the exact watch URL and starts audio playback immediately.

---

## 4. FastAPI Pydantic Response Model Stripping

- **The Pitfall:** Adding database columns (e.g. `key`, `bpm`, `energy`, `brightness`, `genre`) to `list_tracks()` in `api.py` appears to work in SQLite, but `GET /api/tracks` returns `null` or omits those fields in HTTP responses.
- **The Solution:** FastAPI's `response_model=list[TrackResponse]` silently strips any dictionary keys not explicitly declared as fields in `TrackResponse`. Always add new response fields to the Pydantic schema:
  ```python
  class TrackResponse(BaseModel):
      track_id: int
      artist: str
      title: str
      key: Optional[str] = None
      bpm: Optional[float] = None
      energy: Optional[float] = None
      brightness: Optional[float] = None
      genre: Optional[str] = None
  ```

---

## 5. Textual Event Handler Shadowing & Duplicate Widget IDs

- **The Pitfall 1 (Shadowed Handlers):** Declaring two methods with the exact same name `on_data_table_row_selected()` inside a Textual `App` class causes the second method to silently shadow the first. Row clicks in the first table (`#library`) stop firing.
  - **Fix:** Consolidate into a single handler that inspects `event.data_table.id`:
    ```python
    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if getattr(event.data_table, "id", "") == "queue":
            self.play_queue_index(event.cursor_row)
        else:
            self.action_select()
    ```
- **The Pitfall 2 (Duplicate IDs):** Declaring duplicate widget IDs (`id="mood-select"`) across different DOM containers (e.g., sidebar and overlay) causes Textual screen mount crashes and leaves tables unpopulated.
  - **Fix:** Keep widget IDs strictly unique across the entire DOM tree.

---

## 6. Serving Textual Apps over Web (`textual_serve`)

- **The Pitfall:** Invoking `python -m textual serve "python -m karaoke.tui"` launches Textual's built-in welcome/demo app instead of the karaoke TUI.
- **The Solution:** Import and invoke `textual_serve.server.Server` directly in Python:
  ```python
  from textual_serve.server import Server

  server = Server(f"{python_bin} -m karaoke.tui", title="Karaoke TUI", port=8001, host="0.0.0.0")
  server.serve()
  ```

---

## 7. Track Analysis Versioning & `source_kind`

- **Database Table:** `track_analysis_history` (history_id AUTOINCREMENT PK, track_id, source_kind, detected_key, key_confidence, key_agreement, bpm, method, energy, brightness, created_at).
- **Execution:** Every call to `track_analysis.save_detected()` updates canonical `track_analysis` AND appends an immutable row to `track_analysis_history`.
- **`source_kind` Labels:** `sample`, `recording`, `youtube_cache`, `local`, `postprocess`, `cli`.
- **API Endpoint:** `GET /api/tracks/{track_id}/analysis/history` and `ApiClient.get_track_analysis_history(track_id)`.
