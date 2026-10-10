# PostgreSQL mirror, bulk vector rebuilds, and kiosk fallback pitfalls

Session-derived notes from migrating the local karaoke SQLite database to a system PostgreSQL 18 mirror and optimizing vector rebuilds.

## PostgreSQL mirror/migration

Current code still treats SQLite as the live operational backend unless a real dialect layer is implemented. `Settings.uses_postgres` exists, but `localcache.connect()` deliberately rejects `KARAOKE_DB_BACKEND=postgres` to avoid split-brain writes.

A useful interim pattern is a PostgreSQL mirror for benchmarking/future frontend prep:

1. On Gentoo, installing PostgreSQL may not initialize the cluster. If `postgresql-18.service` fails with `Directory not found: /var/lib/postgresql/18/data`, initialize it with:
   ```bash
   sudo emerge --config dev-db/postgresql:18
   sudo systemctl start postgresql-18
   sudo systemctl enable postgresql-18
   ```
2. Create a local DB/user, then load from SQLite with a dedicated migrator rather than trying to point the app at Postgres yet.
3. Migration needs to handle SQLite permissiveness that PostgreSQL rejects:
   - orphan child rows (filter or repair before insert when adding FKs);
   - embedded NUL characters in TEXT (strip or transform before insert);
   - sequence reset after explicit ID inserts (`setval(...)`).
4. Make the migrator idempotent (`TRUNCATE ... CASCADE`, reload, reset sequences) so it can be rerun after a long SQLite ingestion finishes.

Benchmarks from this session: local in-process SQLite was faster for large result-set FE reads (`library_browse`, smartlist candidates), while PostgreSQL won small aggregate/filter queries. Do not assume Postgres speeds up the current local TUI; adopt it for multi-client/concurrent-web reasons, not single-user local latency.

## Bulk OpenSearch vector rebuilds

The old vector rebuild path used `c.index(...)` once per track/line/note document. For a full `--lines` rebuild this means ~200k sequential HTTP requests and can take close to an hour.

Preferred pattern:

- Generate document actions lazily so only one track's line embeddings are held in memory.
- Flush to OpenSearch `_bulk` in modest chunks (default 500 docs) instead of per-doc requests.
- Count document-build errors separately from transport-level bulk item failures.
- Add a CLI thread cap (e.g. `--threads 2` or `--threads 4`) before torch/sentence-transformers import so embedding does not saturate every core and starve the TUI/web-TUI.

Observed write-path benchmark: `_bulk` was ~16x faster than per-document indexing for already-built docs (`~0.10 ms/doc` vs `~1.59 ms/doc`).

Safe rebuild command while web-TUI is in use:

```bash
PYTHONPATH=src .venv/bin/python -m karaoke.vector_index --rebuild --lines --notes --threads 4 --chunk-size 500
```

Drop to `--threads 2` if the UI still feels slow. The process lock should prevent concurrent full vector rebuilds.

## Kiosk playback: avoid stray normal Chrome windows

`open_song_url()` should prefer navigating the dedicated kiosk Chrome over CDP. If CDP is slow/unavailable under load, falling back to `xdg-open` for browser/streaming URLs launches the user's normal browser profile (observed as a stray `/opt/google/chrome/chrome` window, sometimes appearing as a small/window title like `a`).

Pitfall/fix:

- Do not silently fall back to `xdg-open` for HTTP(S), YouTube/YT Music, or Spotify URLs.
- Log a warning and return if kiosk navigation/launch fails.
- Keep an explicit escape hatch for debugging only, e.g. `KARAOKE_ALLOW_XDG_OPEN_FALLBACK=1`.
- Confirm the culprit by checking `~/.local/share/karaoke/logs/karaoke.log` for `Executing: xdg-open ...` and `xdg-open.stdout.log` for `Opening in existing browser session`.

This prevents desktop-browser clutter and reduces resource contention with the web-TUI.
