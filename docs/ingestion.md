# Bulk collection ingestion

How to ingest a large local music collection into the karaoke database, track
progress, and recover files that were skipped.

## Scripts

| Script | Purpose |
|---|---|
| `scripts/overnight_collection_scan.py` | Full first-pass scan of the whole collection root. |
| `scripts/retry_skipped_collection.py` | Resumable second pass: re-ingests only files not yet in the DB. Supports `--dry-run`. |

Both run the same pipeline per file:

```
tags → (fingerprint if tags missing) → key/BPM/energy analysis
     → CLAP genre vector (when audio model available)
     → YouTube + Spotify source resolution
     → PostgreSQL ingest (track, local source, lyrics, analysis, genre)
```

The overnight scan rebuilds the OpenSearch vectors once at the end; the retry
pass rebuilds them only if it ingested anything.

Logs:

- Overnight: `~/.local/share/karaoke/logs/overnight_collection_scan.log`
- Retry: `~/.local/share/karaoke/logs/retry_skipped_collection.log`

Follow live with `tail -f <logfile>`.

## Checking ingestion stats

The database is the source of truth for how far ingestion has got. Quick counts:

```bash
psql "$KARAOKE_PG_URL" -c "
  SELECT 'tracks' AS metric, count(*) FROM tracks
  UNION ALL SELECT 'local source', count(DISTINCT track_id) FROM sources WHERE kind='local'
  UNION ALL SELECT 'youtube source', count(DISTINCT track_id) FROM sources WHERE kind IN ('youtube','youtube_music')
  UNION ALL SELECT 'spotify source', count(DISTINCT track_id) FROM sources WHERE kind='spotify'
  UNION ALL SELECT 'analyzed', count(*) FROM track_analysis;"
```

Progress against the collection on disk (how many files still need a local
source row) is exactly what the retry pass reports in dry-run:

```bash
PYTHONPATH=src .venv/bin/python scripts/retry_skipped_collection.py --dry-run
# -> On disk=<N> already ingested(local)=<M> missing=<N-M>
```

### Historical example snapshot (before the PostgreSQL cutover)

The following 2026-09-09 measurements came from the old SQLite deployment.
Its `database is locked` failures are historical, not current PostgreSQL behaviour.

A ~20k-file backup collection, partway through the overnight scan:

| Metric | Value |
|---|---|
| Files on disk | 20,293 |
| First-pass position | ~11,800 / 20,293 (~58%) |
| Tracks in DB | 10,412 |
| With local file source | 9,236 |
| With YouTube / YT Music source | 8,882 |
| With Spotify source | 815 |
| Key/BPM analyzed | 9,904 |
| Skipped on `database is locked` | 107 |
| Other errors | 1 |

The historical run achieved roughly 800 files/hour, dominated by online
source lookups. Current performance depends on the workload, external providers
and PostgreSQL; SQLite WAL tuning does not apply.

## Recovering skipped files (resumable)

Files skipped because of a database/provider error or missing usable metadata
may have no `kind='local'` source row. The retry pass compares files on disk with
PostgreSQL and re-ingests missing ones. The old SQLite `database is locked`
condition is not a current-backend diagnostic:

```bash
# Preview what would be retried (read-only)
PYTHONPATH=src .venv/bin/python scripts/retry_skipped_collection.py --dry-run

# Run for real
PYTHONPATH=src .venv/bin/python scripts/retry_skipped_collection.py
```

It is **resumable**: each run recomputes the missing set from the database, so
you can stop it (Ctrl-C) and relaunch later and it continues where it left off.
Run the retry pass **after** the first pass finishes to avoid duplicate work
and competing downloads or provider lookups. PostgreSQL supports concurrent
clients; there is no SQLite single-writer/WAL configuration to tune.

### Running from Admin TUI

The Admin TUI (`python -m karaoke.admin_tui`) exposes these operations directly in the **File Ingestion & Folder Scan** panel:

- **`Retry Skipped (p)`** (`btn-retry-skipped`): Ingests skipped files for the specified or default collection folder, updating OpenSearch vectors on completion.
- **`Dry Run Check (d)`** (`btn-retry-dryrun`): Rapidly previews how many on-disk files are missing from PostgreSQL without modifying anything.
- **`Index YT Cache (y)`** (`btn-index-cache`): Scans downloaded YouTube cache files (`~/.local/share/karaoke/youtube/`) and adds unindexed tracks to PostgreSQL.

### Files that still won't ingest

The scanner already tries a filename-based `Artist - Title.ext` fallback when
tags and fingerprint metadata are incomplete. Files that still lack usable
artist/title metadata are skipped; inspect their names/tags rather than assuming
that retrying will identify them. See `folder_scan.py` for the fallback order.
