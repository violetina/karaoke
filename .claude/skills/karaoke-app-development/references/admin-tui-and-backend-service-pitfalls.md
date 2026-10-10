# Admin/backend TUI, systemd service refresh, and FastAPI response pitfalls

Durable lessons from building the backend operations TUI (`make admin`,
`karaoke.admin_tui`) and wiring new control-API endpoints.

## Pitfall: systemd services run CACHED code — restart after ANY code change
`karaoke-api.service` (:8000) and `karaoke-ctrl-api.service` (:8765) are
long-lived `systemd --user` processes. After you add or change an endpoint or
any imported module, the running process still executes the OLD code until
restarted. Classic symptom: a brand-new endpoint returns **404 Not Found** and a
client (e.g. the admin TUI) shows empty/loading data even though the code is
correct on disk.
- Fix: `systemctl --user restart karaoke-api.service karaoke-ctrl-api.service`
  after editing anything they serve, THEN re-test the live endpoint with `curl`.
- Diagnose: `curl -fsS http://localhost:8765/api/<new-endpoint>` → 404 while the
  code clearly defines it ⇒ stale process, not a routing bug.
- Make TUIs resilient to this: give admin panels a **direct fallback** so they
  still work when the control API is un-restarted or down. Example: the worker
  panel falls back to `systemctl --user is-active karaoke-postprocess@1..6.service`
  when `GET /api/workers/status` is unreachable/404.

## Pitfall: FastAPI `response_model` silently strips fields
An endpoint declared `@app.get(..., response_model=list[TrackResponse])` filters
the returned dict down to the Pydantic model's fields. Adding a key to the
handler's return dict (e.g. `genre`, `play_count`, `energy`) does NOT surface it
over HTTP until you ALSO add it to the `TrackResponse` model. This is why
HTTP-mode `ApiClient` clients saw missing analysis fields while the in-process
SQLite fallback saw them. Always update the response model alongside the query.

## Long-running admin actions: run off the UI thread + guard concurrency
Heavy operations (audio backfill, OpenSearch vector rebuild, recording analysis,
Whisper align) must not block the Textual event loop:
- Dispatch with `self.run_worker(_bg, thread=True)`; marshal every UI update back
  with `self.call_from_thread(self.notify, ...)`.
- Guard against double-runs at TWO levels:
  1. **TUI-level** in-flight flag (`_bg_busy`): first action sets it, a second
     is refused with a warning until the first's `finally` clears it. Use
     `getattr(self, "_bg_busy", False)` so `__new__`-constructed test instances
     (no `__init__`) don't raise.
  2. **Engine-level** `threading.Lock` inside `vector_index.rebuild_from_sqlite`:
     `if not _REBUILD_LOCK.acquire(blocking=False): return VectorIndexStats()`.
     This protects ALL callers (TUI, scripts, recording_worker), not just the UI.
- Rebuild is inherently idempotent anyway: docs are written by deterministic id
  (`track_doc_id`), so a repeated/overlapping run overwrites the same documents
  rather than duplicating them. Worst case without the lock is wasted CPU, not
  corruption. Verify health with `curl -s localhost:9200/_cat/indices?v` —
  `green` + `docs.deleted: 0` means fine.

## Whisper alignment of user-supplied plain text (admin panel)
To force-align pasted lyrics or an uploaded `lyrics.txt` to a track:
1. Resolve the track (id, `Artist - Title`, or `localcache.find_track_id`).
2. Store plain text: `localcache.add_track_and_lyrics(artist, title,
   localcache.Lyrics(synced_raw="", plain=text, source="user_input"), conn=conn)`.
   Note `Lyrics` has no `kind` field — do not pass one.
3. Get/locate audio (local path, else `youtube.download(url)`).
4. Run `postprocess_worker._run_sync(track_id, Path(audio), conn)` — this is the
   canonical timing-only aligner (`lyric_align.align_lines`), storing
   `whisper_aligned` (real words) vs `whisper_synced` (transcribed words) per
   `librarysearch.is_transcribed`.
5. `vector_index.rebuild_from_sqlite(embed=True, include_lines=True)` to reindex.

## Play counts + "least played" priority
- `tracks.play_count` column added via idempotent `ensure_play_count_column()`
  migration in `localcache.connect()`, backfilled from `play_events`
  (`event IN ('play','discover')`), and incremented inside `log_event` (the
  event logger is `log_event`, NOT `record_event`).
- Expose `play_count` in `list_tracks` + `TrackResponse`; TUI sort options
  `least_played` (`ORDER BY play_count ASC` — surfaces undiscovered tracks) and
  `most_played`.

## Chrome CDP: bypass the "Leave site?" beforeunload prompt on navigation
YouTube Music registers a `beforeunload` handler during playback, so a CDP
`Page.navigate` (e.g. pressing `n`/next) pops a blocking "Leave site?" modal and
stalls. In `player_open.try_chrome_cdp_navigate`, before navigating:
`Runtime.evaluate` `window.onbeforeunload = null; window.onpagehide = null;`,
then `Page.handleJavaScriptDialog {accept: true}` before AND after
`Page.navigate`, with a JS `location.href` fallback. Auto-advance never triggers
the prompt because it does not unload the page; only explicit navigations do.

## textual-serve (web TUI) correct invocation
`python -m textual serve "..."` launches Textual's built-in DEMO app, not your
app. Use the library API instead: `from textual_serve.server import Server;
Server(f"{python} -m karaoke.tui", host=..., port=...).serve()`. Pick a port that
does not collide (karaoke-api uses :8000; the web TUI and mkdocs must each get
their own — docs moved to :8085).

## Textual specifics that bit this session
- Duplicate widget `id=` (e.g. two `#mood-select`) crashes the mount and can
  leave a whole panel blank. Keep ids unique across the compose tree.
- `_render_synced`/tick callbacks query `#lyrics` etc.; wrap `query_one` in
  try/except and return early — during resize/unmount the node can be missing
  and raise `NoMatches`, killing the timer.
- Known flaky: `tests/test_tui_layout.py::test_resizing_redraws_the_header`
  passes in isolation but can fail interleaved in the full run. Re-run alone to
  confirm; do not "fix" by chasing the interleaving.
- Live playhead for lyric timing: prefer `player_open.browser_playback()`
  (`<video>.currentTime` over CDP :9222) over `playerctl.position()`, which
  Chrome updates only intermittently and can freeze (stuck "next in 0.5s").

## Live Kiosk Chrome Management via Systemd
- **Dedicated Kiosk Service:** Chrome browser session for playback is managed via a user-level unit (`karaoke-kiosk.service` running on display `:0`).
- **Decoupled Playback:** Removed inline Chrome spawning from `make tui` in the `Makefile`. Startup is fully asynchronous and managed cleanly via systemd or the background control API.
- **Robust Restart (Shortcut `K`):** Pressing `K` in the Admin TUI triggers a restart via the control API, which attempts running `systemctl --user restart karaoke-kiosk.service` first, then falls back to killing any active CDP-enabled Chrome processes (`port 9222`) and launching a fresh, isolated background process.

## Live Recordings & In-place Processing in Admin TUI
- **Consolidated Live Record Control (Shortcut `O`):** Unattended live recording toggle has been moved from the main playback TUI to the Admin TUI under the `O` key and `btn-record` widget.
- **Blinking REC Indicator:** Updates every 3s via a `#record-panel` widget in the Admin TUI, rendering a live blinking `● REC` indicator with active session clock, total audio file size, mark ratio, and monitor source.
- **Recent Recordings Table (`recordings-table`):** Lists the last 15 recording entries dynamically, displaying formatted timestamps, active/completed statuses, and identified song counts.
- **Enter-to-Ingest:** Pressing `Enter` on any completed recording row in the `DataTable` spawns a background thread that decompiles and processes that specific selected recording ID (`recording_worker.analyse(rid)`), ingests the track metadata/audio vectors, rebuilds the OpenSearch indexes, and refreshes the table status in place.
