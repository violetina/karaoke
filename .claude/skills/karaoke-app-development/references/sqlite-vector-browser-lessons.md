# SQLite, browser selection, and vector indexing lessons

Session lessons for the local `karaoke` terminal app.

## Durable architecture rule

SQLite is the operational source of truth for playback and automation:

- `tracks`: canonical artist/title/album/duration identity.
- `sources`: YouTube URLs, Spotify URIs, local paths, and player source metadata.
- `lyrics`: approved/staged/rejected plain and synced lyrics.
- `play_events`: stats/radio/player history.
- `lyric_gaps`: missing-lyrics backfill queue.

OpenSearch should be treated as a derived/rebuildable vector index, not as the hot playback/cache dependency. If OpenSearch is down, normal playback, browsing, radio stats, source lookup, and backfill queueing should keep working from SQLite.

## Source URL pitfall

When caching lyrics from a `SongRef`, preserve `SongRef.url` into the SQLite `sources` table. Otherwise interactive browsing and URL-based player sync show songs but cannot start/open them.

Check with:

```bash
python - <<'PY'
from karaoke import localcache
conn = localcache.connect(); cur = conn.cursor()
for row in cur.execute('''
select t.track_id,t.artist,t.title,coalesce(s.kind,'') kind,coalesce(s.url,'') url
from tracks t left join sources s on t.track_id=s.track_id
order by t.artist,t.title
'''):
    print(dict(row))
conn.close()
PY
```

## Browse behavior

The TUI browser should not silently do nothing for tracks without a stored URL. Use the stored source URL when present; otherwise fall back to opening a YouTube search for `artist title`. This keeps old radio/history rows usable even before exact sources are learned.

## YouTube module safety

Do not replace `src/karaoke/youtube.py` with a short helper file. It contains important title parsing, cookie handling, cache reporting, pruning, and `resolve_youtube()` behavior. If adding helpers for backfill, add small wrappers (`search`, `download`) around the existing implementation instead of overwriting it.

## Vector search plan

Re-add semantic/vector features as SQLite-derived indexing:

- `karaoke-vector-index --dry-run --no-embed --lines`
- `karaoke-vector-index --rebuild`
- `karaoke-vector-index --rebuild --lines`
- `make vector-index-dry-run`
- `make vector-index LINES=1`

Track docs come from SQLite `tracks` + preferred `sources` + approved `lyrics`. Line docs are derived from parsed LRC and can later support sentiment, timing, instrumental-gap, and line-break training experiments.

## Verification pattern

Before claiming success, run at least:

```bash
python -m py_compile src/karaoke/youtube.py src/karaoke/backfill_runner.py src/karaoke/player.py src/karaoke/browse.py src/karaoke/cli.py src/karaoke/vector_index.py
python -m pytest
make vector-index-dry-run
```

Use `--dry-run --no-embed` for vector-index verification when OpenSearch or the embedding model may not be available.
