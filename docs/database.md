# Database backend

The karaoke platform stores its operational data (tracks, sources, lyrics,
analysis, genre/tone, recordings, play stats) in a local **SQLite** database at
`~/.local/share/karaoke/karaoke.db`. OpenSearch remains a *derived* index used
only for semantic/vector search and is always rebuilt from this database.

## SQLite (default)

SQLite is the default and the only fully-wired backend. For large collections it
is tuned automatically on every connection:

- **WAL journal mode** — readers (library browse, search, the TUI) run
  concurrently with writers (folder scan / overnight import) instead of blocking
  on a single global lock.
- **`synchronous = NORMAL`** — the safe, fast pairing for WAL.
- **In-memory temp store** and a ~16 MB page cache.
- **Indexes** on the hot join/filter columns: `sources.track_id`,
  `sources.kind`, `lyrics.track_id`, `lyrics(track_id, kind)`, `tracks.artist`,
  `tracks.title` (plus the pre-existing recording/stats/genre indexes).

These changes are created idempotently on connect, so existing databases are
upgraded in place — no manual migration needed.

### Tuning knobs

| Env var | Default | Purpose |
|---|---|---|
| `KARAOKE_DB_BACKEND` | `sqlite` | Backend selector. `postgres` is reserved (see below). |
| `KARAOKE_SQLITE_JOURNAL` | `WAL` | Force a journal mode. Set to `DELETE` if the DB lives on a filesystem that does not support WAL (some network mounts). |
| `KARAOKE_DATA_DIR` | `~/.local/share/karaoke` | Location of `karaoke.db` and caches. |

If SQLite still feels slow after a very large import, first confirm WAL is
active:

```bash
sqlite3 ~/.local/share/karaoke/karaoke.db 'PRAGMA journal_mode;'   # -> wal
```

## Postgres (planned)

For collections large enough that concurrent write throughput or multi-client
access matters, a Postgres backend is planned. It is **not implemented yet** —
setting `KARAOKE_DB_BACKEND=postgres` currently raises a clear error rather than
silently splitting your library across two stores.

The configuration surface is already reserved:

| Env var | Example | Purpose |
|---|---|---|
| `KARAOKE_DB_BACKEND` | `postgres` | Select the Postgres backend (once implemented). |
| `KARAOKE_DB_URL` | `postgresql://karaoke@localhost/karaoke` | Connection string for the system Postgres. |

### Why it is a phased effort

SQLite specifics are currently woven through ~50 modules: `?` parameter
placeholders, `AUTOINCREMENT`, `INSERT OR REPLACE`, `ON CONFLICT`,
`executescript`, `last_insert_rowid()`, and `PRAGMA`. A correct Postgres backend
therefore requires:

1. A connection factory keyed on `KARAOKE_DB_BACKEND` (SQLite default).
2. A SQL dialect layer (placeholder style `?` vs `%s`, upsert syntax,
   auto-increment vs `SERIAL`/`IDENTITY`, `last_insert_rowid()` vs `RETURNING`).
3. Schema DDL translated from the SQLite `CREATE TABLE`/`CREATE INDEX` blocks.
4. A one-time migrator that copies an existing `karaoke.db` into Postgres.
5. Backend-parameterised tests (run the suite against both).

SQLite stays the default throughout; Postgres is opt-in for bigger libraries.
