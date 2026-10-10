# Porting CLIs/TUIs onto ApiClient + test pitfalls

Companion to `references/api-first-and-tui-client.md`. That file covers the
two-service split (`karaoke.api` :8000 read-only, `karaoke.ctrl_api` :8765
desktop-bound) and `ApiClient`'s HTTP-first / in-process-fallback design. This
file adds the CLI/TUI porting patterns and test traps found while migrating
`browse`, `stats`, and `recording` onto the client.

## Porting non-TUI entry points onto ApiClient
Route CLIs through the same client so a future web UI and the CLIs read identical
data:
- `browse.KaraokeBrowser` — `load_songs()` via `api.list_tracks()`, Enter via
  `api.play()`; fall back to the source-preference SQL only when the API returns empty.
- `cli.stats_main` — `api.get_stats(limit=, days=)`. When the CLI needs a filter,
  **extend the endpoint's query params** (`GET /api/stats?limit=&days=`) rather than
  bypassing the API, so CLI and dashboard stay in lockstep.
- `recording_worker.recording_main` — route the *mutating* actions (`--analyse`,
  `--discard`) through `api.record_analyse` / `record_discard_audio`; keep
  `--list`/`--show` on local pretty-print (they emit formatted text, not JSON).

## Synchronous fallback for CLI-facing actions
An HTTP endpoint may run work in the background (`analyse` returns
`{"status":"accepted"}` immediately), but a CLI expects the result now. So
`ApiClient.record_analyse`'s *fallback* runs `recording_worker.analyse(...)`
synchronously and returns `{"status":"analysed","lines":[...]}`; the CLI prints
those lines. Match this shape (background over HTTP, synchronous + result-lines in
fallback) when porting any "returns immediately over HTTP but CLI wants the output"
action.

## Pitfall: a live API server shadows a test's temp DB
On a dev box where `karaoke-api`/`karaoke-ctrl-api` are actually running, an
`ApiClient` call inside a test hits the **real server and live DB** instead of the
test's monkeypatched `localcache.connect(tmp_db)`. Symptom seen: `test_browse`
suddenly saw 808 real tracks instead of its 1 fixture row and failed
`assert len(...) == 1`.

Fix: force the fallback in the test by patching the *client method* to return
empty/None so the SQLite path under test actually runs, e.g.
`monkeypatch.setattr(app.api, "list_tracks", lambda *a, **k: [])`. Monkeypatching
`localcache.connect` alone is NOT enough once the code path prefers the API.

This flips into a positive verification too: if a CLI ported onto `ApiClient`
prints live data on the dev box, that confirms the HTTP path (not just the
fallback) actually works end to end.
