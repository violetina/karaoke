# Ingestion and vector runtime lessons

## Dedicated ingestion logs can freeze while the scan keeps running

Observed in the karaoke runtime: `overnight_collection_scan.log` stopped at 01:15 even though the importer process was still active and SQLite counts were increasing. Root cause was the progress writer doing `print(..., flush=True)` before appending to the logfile. After the launching terminal/pipe closed, `print()` raised `BrokenPipeError`, so the logfile append never happened; `folder_scan` caught the progress callback failure and kept scanning.

Validated diagnosis pattern:

- Compare local time, UTC, and logfile mtime: `date; date -u; stat ~/.local/share/karaoke/logs/overnight_collection_scan.log`.
- Check importer process: `ps -eo pid,ppid,etime,%cpu,%mem,cmd --sort=-%cpu | grep -E 'overnight_collection_scan|folder_scan'`.
- Verify DB counts move over a short interval with SQLite counts for `tracks`, local `sources`, YouTube sources, and `track_analysis`.
- If the dedicated progress log is stale, inspect the general karaoke app log for `Folder scan N/TOTAL` and `BrokenPipeError` entries.

Durable fix pattern for unattended progress writers:

1. Write durable logfile output first.
2. Then print to stdout inside `try: ... except BrokenPipeError: pass`.
3. Avoid letting progress callback failures stop or hide the actual importer state.

## Bulk vector rebuilds should be faster and gentler

The old vector rebuild issued one OpenSearch `index()` request per document, making line-level rebuilds dominated by HTTP round trips. The validated improvement is batching with OpenSearch `_bulk` at a modest chunk size (for example 500 docs) while capping embedding CPU threads so the Web TUI remains responsive.

Operational guidance:

- Prefer a throttled rebuild command when humans are using the TUI/Web TUI: `PYTHONPATH=src .venv/bin/python -m karaoke.vector_index --rebuild --lines --notes --threads 2`.
- Use `--threads 4` only when the machine has spare CPU.
- Bulk writes reduce request count dramatically and are usually less load on OpenSearch than per-doc indexing; CPU starvation usually comes from embedding threads, not the bulk write itself.

## Avoid default-browser fallbacks for playback

When kiosk/CDP navigation is slow or unavailable, falling back to `xdg-open` can spawn the user's normal browser profile as a stray window. Prefer failing closed for browser/streaming playback URLs and logging a warning, with an explicit environment escape hatch only for debugging.