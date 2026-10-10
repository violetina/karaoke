# Browse/queue playback & API-change regressions

Lessons from wiring browse/TUI onto `ApiClient` and adding mood filters. These
are the failure modes that broke playback when the API surface changed.

## Direct watch links MUST be preserved (browse-mode playback)
`player_open.open_song_url(url, kind, *, artist, title, prefer_audio=True)`.
Pitfall: an over-eager `prefer_audio` branch rewrote EVERY YouTube URL into a
`music.youtube.com/search?q=<artist title>` URL — including direct
`watch?v=VID` links. That lands on a search-results page and nothing plays;
browse mode looked "b0rked".

Correct order inside the `is_youtube` branch:
1. `extract_youtube_id(url)` first. If a video id exists →
   `https://music.youtube.com/watch?v=VID` (plays immediately, audio side).
2. Only if there is NO video id and artist/title are known → search URL.
3. Else carry over an existing `/results?search_query=` query.
Test both cases: direct-watch → watch URL, empty url → search URL.

## list_tracks must rank sources, not pick arbitrarily
A plain `LEFT JOIN sources s ON s.track_id = t.track_id` + `GROUP BY t.track_id`
returns an ARBITRARY source row. For a track with both a spotify row and a
youtube row, SQLite may hand back the spotify/null url → browse gets `url=None`
→ falls back to a search page. Always select the source via a priority subquery
(`youtube_music > youtube > http > spotify`, then source_id) — the same ranking
`browse.py`/`_load_tracks` already use. Keep API and TUI/browse SQL identical.

## Duplicate method name silently shadows (Python/Textual)
Two `def on_data_table_row_selected(...)` methods in the same `App` class: the
later definition silently overrides the earlier one. Result: the queue handler
shadowed the library-overlay handler, so selecting a row in `H` mode did
nothing while queue rows worked. Fix = ONE dispatcher keyed on
`event.data_table.id` ("queue" plays the row, else `action_select()`).
General rule: grep for duplicate `def on_...`/`def action_...` names before
adding another handler in a large TUI class.

## ensure_schema before joining track_analysis
`track_analysis` (and `track_analysis_history`) is created lazily by
`track_analysis.ensure_schema(conn)`. Any query that JOINs it — `api.list_tracks`,
`tui._load_tracks`, `browse.load_songs` fallback — must call `ensure_schema(conn)`
first, or throwaway/temp test DBs raise `sqlite3.OperationalError: no such table:
track_analysis`. This bit the whole test_tui_layout suite at once.

## CDP :9222 down → playback opens a new tab, not the window
`open_song_url` tries `try_chrome_cdp_navigate(url)` first (reuse the kiosk
window). If the kiosk window was closed, CDP is down, so it falls back to
`xdg-open` → a fresh tab in the default browser instead of in-window playback.
Diagnose with `curl -s --max-time 3 http://localhost:9222/json/version`. A stale
`playerctl` MPRIS name can linger without the debug port. Relaunch the kiosk
window (`make tui` auto-launches it; or `make web --start-kiosk`; or chrome with
`--remote-debugging-port=9222 --user-data-dir=~/.local/share/karaoke/kiosk-chrome`).

## Mood/energy filtering (browse + TUI)
Energy lives in `track_analysis.energy` (0..1). When null, estimate from bpm:
`min(1.0, max(0.2, (bpm-60)/100))`. Bands: 🔥 high >0.75, ⚡ groovy 0.40–0.75,
🌙 mellow <0.40. In the main TUI the mood control is a `Select` UNDER the search
box (always visible) — not buried in the `H` overlay, or the user can't find it.
It filters both the library list and search results (`_apply_mood_filter`).

## Analysis versioning
`save_detected(..., source_kind=...)` upserts the canonical `track_analysis`
row AND appends an immutable `track_analysis_history` row. Pass a source_kind:
sample / folder_scan(local|youtube_cache|recording) / recording / postprocess /
cli. `ensure_schema` backfills existing rows into history once. Exposed via
`GET /api/tracks/{id}/analysis/history` and `ApiClient.get_track_analysis_history`.

## OpenSearch vector query shapes (verified this session)
- `tracks` dense field: `lyrics_vector` (384-d, cosine) — track-level semantic.
- `tracks-lines` dense field: `line_vector` (384-d) — per-lyric-line; hits carry
  `start_s` for jump-to-timestamp.
- `karaoke-clap` field: `clap_vector` (512-d) — zero-shot audio/instrument search
  via `clap_vector.embed_text(prompt)`.
knn body: `{"size":k,"query":{"knn":{"<field>":{"vector":[...],"k":k,
"filter":{"bool":{"must_not":[{"term":{"artist.raw":"X"}}]}}}}}}`. Embed track/
line queries with SentenceTransformer `all-MiniLM-L6-v2`.
