# Resumable local ingestion gap fill

The overnight folder scan walks every file and redoes expensive work regardless
of what is already stored, so an interrupted run cannot be cheaply resumed —
rerunning from file 1 repeats audio analysis and online lookups for ~20k files.
The cheap alternative is to invert the problem: discover what is on disk,
subtract what SQLite already knows, and process only the difference.

## Why the gap is computable: disk paths are preserved

Every ingested file keeps its absolute path as a `local` source row, so the
file to track mapping survives an interrupted scan. Verified distribution after
stopping a run at ~98%:

```
local            19,761      <- absolute paths, the join key for gap detection
youtube          15,370
spotify             917
youtube_music       343
youtube_search       16
```

This is what makes offline re-analysis possible: nothing needs re-fetching, and
`stale_rows` (DB paths that no longer exist on disk) is the separate, opposite
problem worth reporting so an unmounted or moved drive is visible instead of
looking like a clean library.

## The pattern

`folder_scan.scan_and_ingest_folder` already accepts `only_paths` — an explicit
set of absolute paths to restrict processing to, still discovered under the
given root. Build that set from a walk minus the known paths:

```python
known = {row[0] for row in con.execute(
    "SELECT url FROM sources WHERE kind = 'local'")}     # read-only connection

missing = [p for p in audio_on_disk(root) if p not in known]

folder_scan.scan_and_ingest_folder(
    root,
    resolve_streaming=False,        # offline by default, see below
    only_paths=set(missing),
    progress=progress,
)
```

Implemented as `scripts/fill_local_gaps.py` in the karaoke repo, with
`--dry-run`, `--limit`, `--online`, `--no-fingerprint`, `--no-classify`.

Design decisions that matter:

- **Open the DB read-only** (`file:{path}?mode=ro`) so the gap scan is safe to
  run while another writer holds the database.
- **Offline by default.** Filling gaps should not spend a YouTube/Spotify lookup
  per file; make network resolution opt-in via `--online`.
- **Reuse the scanner's own audio predicate** (`tags.is_audio`) rather than a
  hand-rolled extension list, or the walk and the scan will disagree about what
  counts as a file.
- Resolve the DB path from `localcache.settings.local_db`. There is no
  `localcache.default_db_path()`.

Measured on this library: `on_disk=20293 known=19761 missing=790 stale_rows=0`,
about 5s per file offline. Note `missing` was materially smaller than the gap
implied by the scan's last logged position, because the interrupted run had
already ingested files past its final log line — always compute the gap, never
infer it from the log.

Bonus effect: files the original scan hard-skipped with
`missing artist/title after tags & fingerprint` get another chance under the
current filename-fallback logic, so a gap pass can recover previously
unrecoverable files rather than merely catching up.

## Pitfall: `tracks` row count is not a progress metric

Sampling `SELECT count(*) FROM tracks` over 60s returned `+1` and implied the
importer had nearly stalled, while the scan was in fact running at full speed.
Most files in that stretch resolved to tracks that already existed, so no new
rows appeared.

Use the scanner's own counter instead — it is the honest metric:

```bash
grep 'Folder scan ' <app log> | tail -1      # -> Folder scan 19702/20293
```

Derive rate from two positions and their timestamps rather than from row counts.
Cross-check with `track_analysis` / `track_genre` growth if you want DB-side
confirmation.

## Stopping a scan safely

`SIGINT` is enough; the importer exited within 10s. Then confirm the database is
clean before rebooting:

```bash
kill -INT <pid>
pgrep -af 'overnight_collection_scan|folder_scan|retry_skipped'   # expect none
```

```python
con.execute("PRAGMA quick_check").fetchone()        # -> ok
con.execute("PRAGMA wal_checkpoint(TRUNCATE)")      # -> (0, 0, 0), -wal now 0 bytes
```

A `-wal` file truncated to 0 bytes with `quick_check: ok` means nothing is
pending and the machine is safe to reboot.
