# TUI, browse/library, and playback pitfalls

Hard-won lessons from wiring the library overlay, mood filters, queue, and
kiosk playback. All verified in-session.

## Textual: duplicate widget `id` silently breaks the mount
Two widgets sharing the same `id=` (e.g. `#mood-select` yielded once in the
sidebar under search AND again inside the `#browse-overlay`) crashes/aborts the
mount. Symptom: a whole panel renders EMPTY — e.g. the library `DataTable`
showed zero rows and the overlay looked broken — with no obvious traceback in
normal use. Diagnose with a headless mount probe:
```python
import asyncio
from karaoke.tui import KaraokeTui
async def go():
    app = KaraokeTui()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        print("mood-select count:", len(app.query("#mood-select")))
        print("library rows:", app.query_one("#library").row_count)
asyncio.run(go())
```
`count: 1` and non-zero rows == healthy. Any id must be unique across the WHOLE
app tree, not just within a container.

## Textual: a duplicate METHOD name silently shadows the earlier one
Two `def on_data_table_row_selected(...)` in the same class → the later
definition wins and the first never runs. This is exactly why selecting a row in
the `H` (library) overlay did nothing while queue rows played: the queue handler
(defined later) shadowed the library handler. Fix: ONE handler that dispatches on
`event.data_table.id` (`"queue"` → `play_queue_index`, else → `action_select`).
When adding an event handler, grep the class for an existing one of that name.

## Source selection: never bare `LEFT JOIN sources`
`GET /api/tracks` (and any library list) must pick ONE source per track by
priority, or SQLite returns an arbitrary source row — often a Spotify/`NULL`-url
row, so browse-mode Enter lands on a search page or fails. Use the ranked
subquery everywhere tracks are listed (api.py list_tracks, tui `_load_tracks`,
browse.py fallback):
```sql
LEFT JOIN sources s ON s.source_id = (
    SELECT s2.source_id FROM sources s2 WHERE s2.track_id = t.track_id
    ORDER BY CASE
        WHEN s2.kind = 'youtube_music' THEN 0
        WHEN s2.kind = 'youtube' THEN 1
        WHEN s2.url LIKE 'http%' THEN 2
        WHEN s2.kind = 'spotify' THEN 3
        ELSE 4 END, s2.source_id
    LIMIT 1)
```

## Playback: preserve direct watch IDs, don't rewrite to search
`open_song_url(..., prefer_audio=True)` must ONLY fall back to a
`music.youtube.com/search?q=...` URL when there is NO extractable video id.
Rewriting a real `watch?v=VID` into a search query lands YouTube Music on a
results list that plays nothing. Order: extract_youtube_id(url) → if vid,
navigate `music.youtube.com/watch?v={vid}` (plays immediately); elif prefer_audio
and artist+title → search; elif `/results?search_query=` → carry the query over.

## Playback: CDP down → new-tab clutter; self-heal it
If the kiosk window was closed, `try_chrome_cdp_navigate` fails and playback
falls back to `xdg-open` (a new tab in the default browser). Detect with
`curl -s --max-time 3 http://localhost:9222/json/version`. `open_song_url` now
calls `launch_kiosk_browser(url)` (Popen chrome `--app=<url>
--remote-debugging-port=9222 --user-data-dir=~/.local/share/karaoke/kiosk-chrome`,
short sleep, re-check `_cdp_page_socket()`) before the xdg-open fallback so a
closed window self-heals. Leftover `chromium.instanceNNNN` in `playerctl
--list-all` does NOT mean CDP is up — check the port.

## track_analysis joins on fresh/temp DBs
Any query that `LEFT JOIN track_analysis` (list_tracks, browse, tui `_load_tracks`)
must call `track_analysis.ensure_schema(conn)` first, or temp DBs in tests raise
`sqlite3.OperationalError: no such table: track_analysis`. `ensure_schema` also
backfills `track_analysis_history` from existing rows on first run.

## Mood/energy filter derivation
Energy comes from `track_analysis.energy`; when null, estimate from BPM:
`min(1.0, max(0.2, (bpm - 60)/100))`, else default 0.5. Bands: 🔥 ≥0.75,
⚡ 0.40–0.75, 🌙 <0.40. Filter the in-memory row list (pre-cached), not the DB, so
slider/preset changes are instant. Apply the same band filter to search results
(`_apply_mood_filter`) so search and browse agree.

## Folder-scan resolution order (untagged files)
tags → YouTube video-id lookup in SQLite `sources` (filenames like
`-1jPUB7gRyg.webm`) → recording-session markers for `seg-*.flac` under
`recordings/` → songrec fingerprint. `tags.AUDIO_EXTS` MUST include `.webm`
(YT cache) or those files are skipped. `localcache.extract_youtube_id` must
handle bare filenames/paths via `Path(url).stem`, not just URLs. When a YT
cache file already has a video id, skip the network `youtube.search` and build
the watch URL directly — makes cache ingestion instant.

## Verify cheaply
- Live API check after a service restart: `curl -fsS
  http://127.0.0.1:8000/api/tracks?q=<name>` and confirm `url`/`kind`.
- Known flaky: `tests/test_tui_layout.py` snapshot/timing cases fail run-to-run
  under the full suite but pass in isolation; don't chase them.
