# Large-collection DB performance, DB backend config, and Web TUI service

Lessons from ingesting a 20k-file disk collection while the TUI/Web TUI browsed
the same SQLite DB.

## SQLite is the slowness/crash bottleneck under concurrent import

Symptom: while a folder scan / overnight importer writes continuously, the TUI
"loads nothing" and "randomly crashes". Root cause is almost always
`sqlite3.OperationalError: database is locked` escaping into a UI refresh
(e.g. a mood/genre/sort selector calling `load_songs()`), which kills the
Textual child under `textual-serve`.

Fixes, all in `localcache.connect()` (created idempotently, upgrades existing DBs
in place):

- `sqlite3.connect(path, timeout=30.0)` + `PRAGMA busy_timeout = 30000` — readers
  WAIT for the writer instead of raising immediately.
- `PRAGMA journal_mode = WAL` + `PRAGMA synchronous = NORMAL` — readers run
  concurrently with the importer's writer. This is the single biggest win.
  Also `PRAGMA temp_store = MEMORY`, `PRAGMA cache_size = -16000`.
- Env override `KARAOKE_SQLITE_JOURNAL` (default `WAL`) → set `DELETE` on
  filesystems that do not support WAL (some network mounts).
- Missing indexes were the other half: the hot JOIN/filter columns had none.
  Add `idx_sources_track (sources.track_id)`, `idx_sources_kind`,
  `idx_lyrics_track`, `idx_lyrics_track_kind`, `idx_tracks_artist`,
  `idx_tracks_title`. Verify with `EXPLAIN QUERY PLAN` — a `SEARCH ... USING
  INDEX` replaces a full-table `SCAN`.

Belt-and-braces at the UI layer too: wrap `_apply_mood_change()` (and any
selector-driven refresh that calls `load_songs()`) in try/except so a transient
busy DB shows a warning notification instead of crashing the TUI child.

## Configurable DB backend (SQLite default, Postgres reserved)

The user wanted "move to Postgres for bigger collections, keep SQLite default,
make it configurable". A TRUE backend swap is a large effort — SQLite specifics
are woven through ~50 modules (`?` placeholders, `AUTOINCREMENT`,
`INSERT OR REPLACE`, `ON CONFLICT`, `executescript`, `last_insert_rowid()`,
`PRAGMA`; ~287 occurrences). Do NOT fake a half-working Postgres path.

What was actually done (safe, honest scaffold):
- `Settings` gained `db_backend` (`KARAOKE_DB_BACKEND`, default `sqlite`) and
  `db_url` (`KARAOKE_DB_URL`), plus `uses_postgres` property.
- `localcache.connect()` raises `NotImplementedError` if postgres is selected
  (only when `db_path is None`; an explicit path always opens SQLite so tests
  and tools keep working). Fail loudly — never silently fall back, which would
  split the library across two stores.
- `docs/database.md` documents tuning knobs and the phased Postgres roadmap:
  connection factory → SQL dialect layer → schema DDL translation → one-time
  migrator → backend-parameterised tests.

Order of operations that worked: ship WAL+indexes first (fixes the real "slow"
complaint with zero schema risk), THEN offer the backend decision. Measure real
feel before committing to the full port.

## Web TUI as a systemd --user service

There was no unit for the browser TUI (`scripts/web_serve.py`, textual-serve on
port 8001). Added `deploy/systemd/karaoke-webtui.service`, wired into
`karaoke.target`, `make systemd-install`, and `systemd-uninstall`. Reload after
Python changes with `systemctl --user restart karaoke-webtui.service`.

Pitfall — restart hangs: textual-serve leaves the child `python -m karaoke.tui`
and WebSocket/keepalive threads pending, so the unit sticks in
`deactivating (stop-sigterm)`. Fix in the unit: `TimeoutStopSec=10` +
`KillMode=control-group` so systemd reaps the whole cgroup. Emergency reset:
`systemctl --user kill --kill-who=all --signal=SIGKILL karaoke-webtui.service`
then `start`. Also tell the user to use `http://localhost:8001/` (textual-serve
advertises `0.0.0.0` which some browsers dislike).

Makefile gotcha: recipe lines MUST be tab-indented. A space-indented
`ln -sf ... webtui.service` line caused `Makefile:NNN: *** missing separator`.

## Startup load cap + no autoplay

For a huge library, cap the first paint and do not auto-start playback:
- `INITIAL_BROWSE_LIMIT` (module const in tui.py) bounds the initial library
  render and the `*` wildcard queue (`ORDER BY RANDOM() LIMIT :n`). User settled
  on ~70 for fast startup.
- Thread `start_playing` through `_background_search(...)` → `_set_queue(...)` so
  the on-mount `*` load populates the queue WITHOUT playing anything.

## Follow-queue authority (o)

Two queues coexist: the local TUI queue and YouTube Music's own temporary `TL...`
queue. If Follow mode is off, YT Music picks the next track (why "Trentemøller
should be playing but something else plays"). Make a populated local queue force
`self._play_once = True` in `_set_queue`, and refuse to toggle it off while the
queue is active. Relabel `o` "Follow Queue"; ytmusicapi cannot clear the live
player queue (playlist CRUD only) — live queue control is CDP-only.

## Duet visual border-split

"Half a person on each side" was the render width not matching the panel: the
frame was built at a fixed 32 cols and textual wrapped it. Measure
`#ascii-visual` `content_size.width` and pass it as `width`; clip left dancer to
the left half and right to the right half, dropping glyphs that would cross the
centre rather than clamping per-column; fall back to one compact dancer when the
panel is too narrow for two full figures.
