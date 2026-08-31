# Local cache and stats

Karaoke keeps a small, always-available SQLite database that is independent of the
OpenSearch/kind cluster. It has two jobs:

1. **Offline lyrics cache** — serve a known song's lyrics without the cluster and
   without a fresh online request.
2. **Play and discovery stats** — record what was played and what radio mode
   discovered, surfaced by `karaoke-stats`.

Default location: `~/.local/share/karaoke/karaoke.db`
(override with `KARAOKE_DATA_DIR`).

## Why a second cache

The OpenSearch index (on the kind cluster) is the rich search/index store, but it
is only available when that cluster is running. Live modes (`--radio`, `--listen`,
`--output`, `--spotify`) should keep working — and keep known songs offline — even
with no cluster. The local SQLite cache fills that gap.

## Lyrics lookup order

`player.get_synced()` now checks caches cheapest-first:

```mermaid
flowchart TD
    A[Need lyrics for artist/title] --> B{use_cache?}
    B -- yes --> L[1. Local SQLite cache]
    L -- hit --> Z[Return lyrics offline]
    L -- miss --> O[2. OpenSearch cluster cache]
    O -- hit --> W[Write-through to local cache] --> Z
    O -- miss/unreachable --> F[3. LRCLIB online]
    F -- found --> W2[Write-through to local + OpenSearch] --> Z
    F -- none + local file + --transcribe --> WH[Whisper transcription] --> W2
    B -- no --> F
```

This is what fixes radio mode: once a song has been recognised and its lyrics
fetched, the **next** time radio re-discovers that same song, the lyrics come
straight from the local cache — no LRCLIB call, no cluster needed.

## Offline song identification (limitation)

The local cache stores the **result** of a recognition (`artist/title -> lyrics`),
not an audio fingerprint. Identifying an *unknown* song from sound still uses
`songrec`, which queries Shazam **online**; there is no offline audio-fingerprint
match today.

True offline audio identification would require a local fingerprint database
(e.g. AcoustID/Chromaprint `fpcalc`, or a self-hosted fingerprint index). That is
a possible future addition; `scripts/soundcheck.sh` flags that songrec is
online-only so the limitation is visible.

## Stats

Every play/identification writes a `play_events` row. `karaoke-stats` aggregates
them.

```bash
karaoke-stats                # summary + top tracks/artists + by-mode
karaoke-stats --days 7       # only the last week
karaoke-stats -n 20          # longer top lists
karaoke-stats --json         # machine-readable
make stats                   # same summary via make
```

Recorded event types:

| Event | Meaning |
| --- | --- |
| `discover` | Radio/live mode identified a new song via songrec. |
| `relock` | Radio re-anchored the same song (drift correction). |
| `play` | A song's synced lyrics started rendering. |
| `cache_hit` | Lyrics served from the local cache. |
| `cache_miss` | Local cache had no lyrics; went to cluster/LRCLIB. |
| `no_lyrics` | No lyrics found anywhere for the track. |

Each row also records the `mode` (`radio`, `spotify`, `listen`, `output`, `file`,
`query`) and lyric `source` (`local`, `lrclib`, `whisper`, `none`), so the summary
can show discovery counts, per-mode activity, and local-cache hit rate.

## Schema

```sql
CREATE TABLE lyrics_cache (
    key           TEXT PRIMARY KEY,   -- normalized "artist\ntitle" (casefolded)
    artist        TEXT, title TEXT, album TEXT, duration REAL,
    lyrics_source TEXT,               -- lrclib | whisper | none
    has_synced    INTEGER,
    plain_lyrics  TEXT, synced_lyrics TEXT,
    updated_at    REAL
);

CREATE TABLE play_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, mode TEXT, artist TEXT, title TEXT,
    event TEXT,                        -- play | discover | relock | cache_hit | cache_miss | no_lyrics
    source TEXT, has_synced INTEGER
);
```

All writes are best-effort: a cache or stats failure never interrupts playback.
