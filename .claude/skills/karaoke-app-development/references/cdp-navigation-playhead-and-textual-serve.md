# CDP navigation, playhead sync, textual-serve, and Textual mount pitfalls

Durable fixes discovered while wiring queue playback, the web TUI, and live lyric sync.

## textual-serve must use the Server API, not `python -m textual serve`
`python -m textual serve "<cmd>"` launches Textual's built-in demo/welcome app,
NOT your app — the web page shows "What is Textual?" instead of the karaoke TUI.
Serve via the library API directly (the `textual-serve` package is installed):

```python
from textual_serve.server import Server
server = Server(f"{sys.executable} -m karaoke.tui", title="Karaoke TUI",
                host="0.0.0.0", port=8001)
server.serve()
```

`scripts/web_serve.py` does this. Verify with `curl -I http://127.0.0.1:8001/` → `200 OK`.

## Port map — keep these from colliding
- `8000` karaoke-api (systemd --user, read-only library API)
- `8001` web TUI (`make web` / textual-serve)
- `8085` mkdocs docs server (`DOCS_ADDR` in Makefile; moved off 8001 which webtui took)
- `8765` karaoke-ctrl-api (host/desktop control API)
- `9222` Chrome kiosk CDP
When adding a new local server, pick a free port and note it here.

## Chrome CDP navigation: bypass the "Leave site?" beforeunload prompt
YouTube Music registers `window.onbeforeunload` during playback. A plain
`Page.navigate` over CDP then blocks on a "Leave site? Changes may not be saved"
modal, so pressing `n`/Next in queue mode hangs waiting for a human click
(while normal in-page next just works). Fix in `try_chrome_cdp_navigate()`:
1. `Runtime.evaluate` → clear handlers: `window.onbeforeunload = null; window.onpagehide = null;`
2. `Page.handleJavaScriptDialog {accept: true}` (before AND after navigate).
3. `Page.navigate`; on failure fall back to JS `window.location.href = url`.

## Live lyric playhead: read the CDP <video> element, not MPRIS
`playerctl position` for a browser returns a *stuck/static* value (e.g. frozen at
0.5s), so the lyric countdown froze at "next in 0.5s" and never advanced. Query
Chrome's own `<video>.currentTime` via `browser_playback()` (CDP `Runtime.evaluate`)
FIRST in `_tick_lyrics`, and fall back to `playerctl.position()` only when CDP is
down. In unit tests, monkeypatch `karaoke.tui.browser_playback` to return None so
the playerctl path is exercised deterministically.

## Textual: duplicate `id` silently blanks widgets
Two widgets sharing the same `id` (e.g. a second `#mood-select` accidentally left
in an overlay) breaks Textual's mount and the affected DataTable renders EMPTY
with no visible error. Symptom: "the list shows nothing". Enforce one id per
widget. Diagnose with a headless mount check counting the id and the table rows:
```python
async with app.run_test(size=(160,50)) as pilot:
    await pilot.pause()
    print(len(app.query("#mood-select")), app.query_one("#library", DataTable).row_count)
```

## Textual: query_one can raise NoMatches mid-layout
During resize / focus-mode / unmount transitions a widget like `#lyrics` may not
be mounted when a timer callback fires (`_render_synced`), crashing the app with
`NoMatches`. Wrap the lookup: `try: w = self.query_one("#lyrics", Static) except Exception: return`.

## Denormalized play_count + idempotent migration + backfill
To sort "priority: least played" cheaply, add a `tracks.play_count` column rather
than aggregating `play_events` on every query:
- `ensure_play_count_column(conn)` (called from `connect()`): `ALTER TABLE ... ADD
  COLUMN play_count INTEGER NOT NULL DEFAULT 0` then a one-time backfill
  `UPDATE tracks SET play_count = (SELECT COUNT(*) FROM play_events WHERE event IN
  ('play','discover') AND LOWER(artist)=LOWER(tracks.artist) AND LOWER(title)=...)`.
- Increment inside `log_event()` on `play`/`discover`.
- Expose in `api.list_tracks` AND add the field to the `TrackResponse` pydantic
  model — FastAPI `response_model` silently DROPS fields not declared on the model
  (this same trap hid key/bpm/energy/genre until they were added to TrackResponse).

## TUI filter/sort composition
Mood/energy filtering and sort both run in Python AFTER the SQL fetch (on
`self._song_data`), so new sort modes are just added to `SORT_OPTIONS` + a branch
in `load_songs`. To also filter the ACTIVE search queue live, keep an
`self._unfiltered_queue` and re-apply the mood filter via a shared
`_filter_and_set_queue()` whenever the slider/preset changes — mood presets should
also sync the numeric slider floor (`high_energy→0.75`, `groovy→0.40`, else `0.0`).
