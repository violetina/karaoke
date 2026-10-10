# When to actually adopt Postgres, and the vector-rebuild bottleneck

Companion to `references/sqlite-to-postgresql-migration.md` (which covers the
one-time migrator + Gentoo setup) and the "Configurable DB backend" section of
`references/large-collection-db-performance-and-resumable-import.md`. This file
captures the *decision* learnings from the session where the user asked to
"wire the FE to Postgres to see if it's faster" while a scan was still running.

## Don't wire the FE to Postgres for "speed" — benchmark first

Two hard facts about this codebase:

- **There is no switch to flip.** `localcache.connect()` deliberately raises
  `NotImplementedError` when `KARAOKE_DB_BACKEND=postgres`. All ~35 FE/backend
  modules talk **raw SQLite** to the connection object: `?` placeholders,
  `INSERT OR IGNORE`/`OR REPLACE`, `ON CONFLICT`, `last_insert_rowid()`,
  `executescript`, `PRAGMA`. Pointing the FE at Postgres is a code change
  (connection factory keyed on `KARAOKE_DB_BACKEND` + a SQL dialect shim:
  `?`→`%s`, upsert syntax, `RETURNING`, `SERIAL`), NOT a config change. The
  migrator is only phase 4 of the 5-phase plan in `docs/database.md`.
- **Measured A/B** (read-only, same migrated data, localhost, median of 7 via
  `scripts/bench_sqlite_vs_postgres.py`):

  | query (hot FE paths in **bold**)              | SQLite | Postgres | winner |
  |-----------------------------------------------|-------:|---------:|--------|
  | **library browse (tracks+source+lyrics ~15.5k)** | 104 ms | 362 ms | SQLite 3.5x |
  | **smartlist load_candidates (~9.6k)**         |  53 ms |  288 ms | SQLite 5.5x |
  | keyword search artist/title LIKE              |  26 ms |   15 ms | Postgres 1.7x |
  | genre facet count (aggregate)                 |  11 ms |  0.6 ms | Postgres 17x |
  | point lookup by artist/title                  |   3 ms |    3 ms | tie |

  In-process SQLite has no client/server per-row serialization + socket cost,
  which dominates large result sets for a single local reader. Postgres only
  wins tiny-result aggregates/scans. So wiring the FE to Postgres would most
  likely make the browse/list screens **slower**, not faster, for local use.

## Adopt Postgres for CONCURRENCY / multi-client, not local latency

The legitimate reasons (which the user's roadmap does hit):

- **Concurrent writers.** SQLite even with WAL serializes writers on a single
  write lock. Concurrent TUI + Web TUI writers is exactly where you hit
  `database is locked` (the overnight scan logged 107 such skips). Postgres has
  real row-level MVCC concurrency.
- **A real web frontend = multiple socket clients**, which SQLite explicitly is
  not built for.

State this plainly rather than justifying Postgres on speed. That is what
justifies building the actual dialect layer + connection factory (phases 1–2 in
`docs/database.md`) rather than a throwaway read-only path.

## "Fill the gap later" is already a one-liner

`scripts/migrate_sqlite_to_postgres.py` is **idempotent**: each table is
`TRUNCATE ... CASCADE`d, reloaded, and its `SERIAL` sequence re-`setval`d. After
the live SQLite scan finishes ingesting, just re-run the migrator and Postgres
matches SQLite exactly — no incremental/delta logic needed. Keep SQLite as the
live write target during ingestion; do NOT disturb a running scan to migrate.

## Vector rebuild is slow: one HTTP request per document

`karaoke-vector-index --rebuild --lines` (src/karaoke/vector_index.py) calls
`c.index(...)` **once per document**. For the line index that's ~210k sequential
HTTP round-trips to OpenSearch at ~13ms localhost latency each → measured
~73 docs/sec, so a full `--lines` rebuild takes **~45–55 min**, dominated by
network round-trips, NOT the sentence-transformer embedding (which pegs ~7 cores
and finishes far sooner). Do not estimate this at a few minutes.

- **Check progress without killing it:** diff `indexing.index_total` from
  `curl -s http://localhost:9200/tracks-lines/_stats/indexing` over ~20s to get
  docs/sec. The process is healthy if CPU is high (~700%) even when it *looks*
  stalled right after the `Loading weights: 100%` line. Rebuild is idempotent by
  stable doc-id, so let a slow run finish rather than interrupt it.
- **The real fix:** switch the rebuild to the OpenSearch bulk helper
  (`opensearchpy.helpers.streaming_bulk`, batches of ~500) — one request per 500
  docs instead of per doc, typically 10–20x faster (minutes, not ~50 min).
  Worth doing since rebuilds recur as ingestion continues.
