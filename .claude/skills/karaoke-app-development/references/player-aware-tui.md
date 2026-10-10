# Player-aware TUI: auto-detect mode, live sync, controls, staging filter

The `karaoke-tui` app (`src/karaoke/tui.py`, entrypoint `karaoke-tui` / `make tui`)
watches the desktop and picks a mode automatically instead of asking the user.

## Detection module (`src/karaoke/detect.py`)
Keep detection pure and testable — pass MPRIS metadata in, get a `Detection` out:

- `classify(meta) -> Detection(mode, player, artist, title, url)`
  - player name starts with `spotify` -> `spotify` mode
  - any other player with a real track/url -> `scan` mode (browser YouTube/YT
    Music tab, VLC, local players)
  - nothing playing -> `browse` mode (library-driven; Enter opens the source in
    the browser — the old "youtube" behavior)
- `detect_active()` = `classify(current_metadata())`.
- `resolve_lyrics(det, conn)` prefers a **URL match against `sources`** (canonical
  for browser tabs) before falling back to normalized artist/title.
- `record_gap(det, conn)` upserts the source (`add_track_source`) and calls
  `localcache.log_lyric_gap(...)` so missing tracks land in the staging/backfill
  queue. Best-effort, never raises.

## playerctl control helpers (`src/karaoke/playerctl.py`)
Added alongside the metadata helpers: `position()`, `status()`, `play_pause()`,
`next_track()`, `previous_track()`, `seek(offset_s, player="")`. All take an
optional `player` target and return `None`/`False` on failure (never raise).
Seek arg format: positive -> `"5+"` (relative forward), negative -> `"-5"`.

## TUI wiring
- Default is automatic: `set_interval(1.5, poll_detection)` picks the mode;
  `set_interval(0.2, tick_lyrics)` advances the clock from `playerctl.position()`.
- In `scan`/`spotify`, reuse `player._render_body(body, tl, elapsed, mood=...)`
  into a `rich.text.Text` and push it into a Textual `Static` — do NOT reimplement
  the highlight. Mood tint via `sentiment.mood_of` on the active line.
- Controls (space/n/p/[/]) route to `det.player` when a player is active.
- Library filter selector: **Working songs** (only tracks with lyrics — the
  default so only playable songs show), All songs, Staging queue
  (`staging.list_staged(status="all")`).
- Only log a gap once per track per session (`self._gap_logged` set) to avoid
  spamming the queue on every 1.5s poll.

## Pitfalls confirmed live
- **Chromium does not expose `xesam:url`** over MPRIS (Firefox usually does).
  When url is empty, URL-keyed lookup can't run and you fall back to the noisy
  browser title (e.g. `"Come (Official Video) - YouTube"`). Detection still works
  (mode=scan) and the gap is queued, but synced-lyric matching for that tab needs
  either a Firefox tab or title cleanup. Consider stripping `" - YouTube"` and
  `"(Official Video)"` before lyric/staging lookup.
- Textual headless test pattern: `patch("karaoke.detect.detect_active", ...)` then
  `async with app.run_test(size=(140,40)) as pilot: await pilot.pause()` and assert
  `app.query_one("#id")`. Runs without a real player or terminal.
