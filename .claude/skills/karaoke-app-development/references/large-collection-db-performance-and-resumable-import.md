# Large-collection DB performance, Web TUI concurrency & resumable import

Lessons from scaling the karaoke platform to a ~20k-file disk import while the
TUI/Web TUI stayed usable. All of this lives on the SQLite backend
(`~/.local/share/karaoke/karaoke.db`), which remains the operational source of
truth.

## Symptom cluster: "slow while indexing" + Web TUI randomly crashes

Root causes, in order of impact:

1. **No WAL journal.** Default rollback-journal mode gives one global write
   lock, so every reader (library browse, `*` search, the Web TUI) blocks behind
   the folder-scan/overnight importer's writes. Fix in `localcache.connect()`:
   ```python
   conn = sqlite3.connect(str(path), timeout=30.0)
   conn.execute("PRAGMA busy_timeout = 30000")
   conn.execute("PRAGMA journal_mode = WAL")     # env override: KARAOKE_SQLITE_JOURNAL
   conn.execute("PRAGMA synchronous = NORMAL")   # safe+fast pairing for WAL
   conn.execute("PRAGMA temp_store = MEMORY")
   conn.execute("PRAGMA cache_size = -16000")    # ~16 MB page cache
   ```
   WAL is the single biggest win. Keep `KARAOKE_SQLITE_JOURNAL` to force
   `DELETE` on filesystems that don't support WAL (some network mounts).

2. **Missing indexes on the hot join/filter columns.** `sources.track_id` and
   `lyrics.track_id` back every library load and `*` browse JOIN; without an
   index each is a full scan of a table that grows with the whole collection.
   Add idempotently on connect (an `ensure_performance_indexes(conn)` helper):
   `idx_sources_track`, `idx_sources_kind`, `idx_lyrics_track`,
   `idx_lyrics_track_kind`, `idx_tracks_artist`, `idx_tracks_title`.
   Verify with `EXPLAIN QUERY PLAN` — you want `SEARCH ... USING INDEX`, not
   `SCAN`. On the live DB this took a 70-row library JOIN to ~0.5 ms.

3. **`OperationalError: database is locked` crashing the Web TUI.** Even with
   busy_timeout, a lock that outlasts the timeout raised out of a filter refresh
   (`_apply_mood_change` → `load_songs` → `localcache.connect`) and killed the
   Web TUI child, which looked like a "random crash". Wrap the refresh in
   try/except, log + `self.notify(...)`, and stay alive:
   ```python
   def _apply_mood_change(self):
       self._render_mood_slider()
       try:
           self.load_songs(); self._show_selected_song()
           if getattr(self, "_unfiltered_queue", None):
               self._filter_and_set_queue()
       except Exception as exc:
           log.warning("mood/filter refresh deferred: %s", exc)
           self.notify("Library is busy importing; filter refresh will retry", severity="warning")
   ```
   Note the pre-existing importer PROCESS keeps its old (pre-fix) connections,
   so its own `database is locked` skips continue in its log until it restarts —
   those skipped files are recovered by the retry pass below, not by the running
   importer.

## Startup latency: cap the first load, don't autoplay

For big libraries the TUI must not pull the whole table or the full `*` wildcard
queue at startup. Use a module constant (e.g. `INITIAL_BROWSE_LIMIT = 70`) and
`LIMIT :browse_limit` on the initial library query; sample the wildcard queue
with `ORDER BY RANDOM() LIMIT :n`. Typed searches still scan the full DB so
nothing becomes undiscoverable. Also thread a `start_playing=False` flag through
`_background_search`/`_set_queue` so the initial load populates the queue
without auto-starting a song (users disliked autoplay-on-load).

## Configurable DB backend — scaffold honestly, fail loud

Postgres is a legitimate ask for very large collections, but SQLite is hard-wired
in ~287 spots across ~50 files (`?` placeholders, `AUTOINCREMENT`,
`INSERT OR REPLACE`, `ON CONFLICT`, `executescript`, `last_insert_rowid()`,
`PRAGMA`). A real backend swap is a phased project, not a config flag. When the
user is away, ship the safe scaffold, do NOT fake a half-working port:

- Reserve `KARAOKE_DB_BACKEND` (default `sqlite`) + `KARAOKE_DB_URL` in config,
  add `settings.uses_postgres`.
- Make `connect()` **raise `NotImplementedError`** when postgres is selected but
  unimplemented — silently falling back to SQLite would split the library across
  two stores. An explicit `db_path=` (tests/tools) always opens SQLite.
- Document the roadmap in `docs/database.md` (dialect layer, DDL translation,
  one-time migrator, backend-parameterised tests).

## Resumable retry pass for a skipped/interrupted import

The importer skips a file whenever its DB write loses the lock; that file never
gets a `sources` row. Recover without re-touching everything:

- Add an `only_paths: set[str] | None` filter to
  `folder_scan.scan_and_ingest_folder` that intersects the discovered files with
  the given set.
- `scripts/retry_skipped_collection.py`: list on-disk audio under the root,
  subtract paths that already have a `kind='local'` source row (`SELECT url FROM
  sources WHERE kind='local'`), feed the difference via `only_paths`, then
  rebuild vectors. **Resumable by design**: each run recomputes the missing set,
  so stop/relaunch just continues. Supports `--dry-run` (read-only) to preview
  counts before committing.
- Caveat surfaced in dry-run: files with empty ID3 tags that fingerprinting
  can't resolve log `SKIP: missing artist/title` — that's a property of those
  files, not a retry bug. A filename-pattern (`Artist - Title.mp3`) fallback
  would rescue most of them.

## Web TUI as a systemd unit (easy reload)

There was no unit for the browser Web TUI (`scripts/web_serve.py`,
textual-serve, port 8001). Add `deploy/systemd/karaoke-webtui.service`, wire it
into `karaoke.target` + `make systemd-install`, and add
`TimeoutStopSec=10` + `KillMode=control-group` — textual-serve otherwise hangs
in `deactivating (stop-sigterm)` waiting on a disconnected WebSocket / child TUI
process, which makes restart look stuck. Recovery when it hangs:
`systemctl --user kill --kill-who=all --signal=SIGKILL karaoke-webtui.service`
then `start`. Reload after code changes with
`systemctl --user restart karaoke-webtui.service`; browse `http://localhost:8001/`
(not `0.0.0.0`).

## Terminal duet visuals: keep each dancer wholly on one side

The mirrored cartwheel duet split a dancer across the panel border ("half on
each side"). Root cause was rendering a fixed 32-col animation into a narrower
`#ascii-visual`, so Textual wrapped it. Fixes that actually worked: measure the
real panel width (`content_size.width`) and pass it to the renderer; hard-clip
the left figure to the left half and the right to the right half; drop glyphs
that would cross the centre rather than overlapping/merging figures; on a panel
too narrow for two whole figures, fall back to a single compact dancer. Never
clamp per-column — clamp the whole glyph box.
