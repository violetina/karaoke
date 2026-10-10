# Large-collection performance, resumable ingestion, and DB backend scoping

Lessons from ingesting a ~20k-file backup collection while the TUI stayed live.

## SQLite is the bottleneck knob, not the schema

When "it is slow" during a big import/index, the cause is almost always two
things, both fixable without touching the schema or moving off SQLite:

1. **No WAL.** Default rollback-journal mode makes every reader (library
   browse, `*` search, the TUI) block behind the importer's single write lock.
   This is what makes the app feel frozen mid-scan.
2. **Missing indexes on the join/filter columns.** `sources.track_id` and
   `lyrics.track_id` back the JOINs behind every library load and `*` browse;
   without an index each is a full table scan that grows with the collection.

Fix — apply on every connection in `localcache.connect()`, best-effort so
opening the DB never fails:

```python
conn = sqlite3.connect(str(path), timeout=30.0)
conn.execute("PRAGMA busy_timeout = 30000")   # wait for the writer, don't crash
conn.execute("PRAGMA journal_mode = WAL")       # readers concurrent with writer
conn.execute("PRAGMA synchronous = NORMAL")     # safe+fast pairing for WAL
conn.execute("PRAGMA temp_store = MEMORY")
conn.execute("PRAGMA cache_size = -16000")      # ~16 MB
# idempotent CREATE INDEX IF NOT EXISTS on sources.track_id, sources.kind,
# lyrics.track_id, lyrics(track_id,kind), tracks.artist, tracks.title
```

- Verify live: `sqlite3 <db> 'PRAGMA journal_mode;'` -> `wal`, and
  `EXPLAIN QUERY PLAN SELECT * FROM sources WHERE track_id=?` should show
  `USING INDEX idx_sources_track`.
- Provide `KARAOKE_SQLITE_JOURNAL` to force `DELETE` on filesystems where WAL is
  unsupported (some network mounts).
- These are created idempotently on connect, so existing DBs upgrade in place —
  no manual migration.

## `database is locked` crashed the Web TUI, not just the importer

Symptom the user reported as "loads nothing / randomly crashes": a mood/genre
filter refresh called `load_songs()` -> `localcache.connect()` while the
overnight importer held the write lock. The raw `OperationalError` escaped and
killed the Web TUI child (textual-serve then looked like it hung on restart).

Two-part fix:
- `busy_timeout=30000` so a reader waits for the importer instead of raising.
- Wrap the filter-refresh path (`_apply_mood_change` -> load/show/filter) in
  `try/except`, log + `notify(...)` a "library is busy" warning, and stay alive.

The residual lock errors in the importer log belonged to the *old* importer
process started before the WAL code was loaded — restart the writer to pick up
WAL. A locked file is skipped, not fatal; the retry pass (below) reclaims it.

## Resumable bulk-ingestion pattern (the retry pass)

The first-pass scan skips a file whenever the write loses the lock or the file
has no usable tags. Those files never get a `kind='local'` source row, which is
exactly the resume marker.

- Add an `only_paths: set[str] | None` filter to `scan_and_ingest_folder` so a
  run targets an explicit set instead of the whole tree.
- The retry script computes the missing set from the DB each run:
  `on_disk_audio_files - {sources.url WHERE kind='local'}`, feeds it as
  `only_paths`, then rebuilds vectors only if it ingested anything.
- **Resumable by construction**: recomputing the missing set every run means
  Ctrl-C + relaunch just continues. WAL makes lock-skips rare on the retry.
- Run the retry pass AFTER the first pass finishes so they don't contend for
  the writer.
- Honest caveat: files with empty ID3 tags that don't resolve via fingerprint
  log `SKIP ... missing artist/title` — a property of those files, not a bug.
  A filename-pattern (`Artist - Title.mp3`) fallback is the real rescue for them.

## Postgres: reserve config, do NOT fake the backend

User asked to "move to Postgres, keep SQLite default, make it configurable."
The honest scoping that matters for a future session:

- SQLite specifics are woven through ~287 call sites across ~50 modules: `?`
  placeholders, `AUTOINCREMENT`, `INSERT OR REPLACE`, `ON CONFLICT`,
  `executescript`, `last_insert_rowid()`, `PRAGMA`. A real Postgres backend
  needs a dialect layer (placeholders `?`->`%s`, upsert syntax, SERIAL/IDENTITY,
  `RETURNING` vs `last_insert_rowid()`), translated DDL, a data migrator, and
  backend-parameterised tests. It is a multi-hour effort, not a config flip.
- What ships safely NOW: `KARAOKE_DB_BACKEND` (default `sqlite`) and
  `KARAOKE_DB_URL`, plus a **fail-loud guard** — `connect()` raises
  `NotImplementedError` if `postgres` is selected, so it never silently splits
  the library across two stores. Document the roadmap in `docs/database.md`.
- Sequencing trap the user proposed: "stop import, migrate to Postgres, resume."
  Because the backend isn't implemented, resuming on Postgres would ingest
  nothing. Correct order: finish (or keep) SQLite, THEN build+test the backend,
  THEN migrate the completed DB. Push back on the harmful order rather than
  executing it.

## systemd Web TUI service

`textual-serve` (`scripts/web_serve.py`) belongs in its own user unit
(`karaoke-webtui.service`, port 8001, `PartOf=karaoke.target`) so code reloads
are just `systemctl --user restart karaoke-webtui.service`. It spawns a child
`python -m karaoke.tui`; on stop it can hang waiting for that child + its
websocket. Add `TimeoutStopSec=10` and `KillMode=control-group` so systemd
reaps the whole process group. Recover a wedged unit with
`systemctl --user kill --kill-who=all --signal=SIGKILL karaoke-webtui.service`.
Makefile tab-indentation matters: a `ln -sf` line pasted without a leading TAB
under `systemd-install` gives `*** missing separator. Stop.`

## Startup responsiveness

Cap the initial library/`*` wildcard load (e.g. 70 rows via
`ORDER BY RANDOM() LIMIT`) and do NOT autoplay on load — the user found both the
full-collection load and the auto-start undesirable. Typed search still queries
the whole DB, so nothing becomes undiscoverable.
