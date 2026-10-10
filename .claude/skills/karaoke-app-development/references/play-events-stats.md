# Play stats: `play_events` schema and direct SQL

The `karaoke` app records every playback/lookup event into the `play_events`
table in `~/.local/share/karaoke/karaoke.db`. `karaoke-stats` is just a
formatter over `localcache.summarize()`, which reads this table. When the CLI
is broken, unavailable, or you need a cut it does not expose (per-session
windows, relock churn, lyric-source breakdown), query the table directly.

## Schema

```sql
CREATE TABLE play_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    mode       TEXT NOT NULL,   -- radio | spotify | player | listen | output | file | youtube | query | print
    artist     TEXT DEFAULT '',
    title      TEXT DEFAULT '',
    event      TEXT NOT NULL,   -- play | discover | relock | cache_hit | cache_miss | no_lyrics
    source     TEXT DEFAULT '', -- local | opensearch | lrclib | whisper | songrec | none
    has_synced INTEGER DEFAULT 0
);
CREATE INDEX idx_play_events_ts    ON play_events (ts);
CREATE INDEX idx_play_events_track ON play_events (artist, title);
```

Semantics worth knowing before interpreting numbers:

- `mode` is the *input* path, not the player. `radio` = continuous songrec
  listening; `player` = MPRIS follow; `spotify` = Spotify API follow.
- `source` on a `discover` row is the **identification** source (`songrec`),
  while on a `play` row it is the **lyrics** source (`lrclib`, `local`,
  `whisper`, or `none`).
- `relock` fires when the player re-anchors position mid-song. A high
  relock:play ratio (seen at ~4.5:1 during a radio session) means sync is
  drifting, not that anything crashed.
- `has_synced=1` means LRC timed lyrics; `0` means plain text only.

## Useful queries

Session window and volume per mode:

```sql
SELECT mode, COUNT(*) n,
       datetime(MIN(ts),'unixepoch','localtime') first,
       datetime(MAX(ts),'unixepoch','localtime') last
FROM play_events GROUP BY mode ORDER BY n DESC;
```

Event breakdown for one mode:

```sql
SELECT event, COUNT(*) n FROM play_events
WHERE mode='radio' GROUP BY event ORDER BY n DESC;
```

Radio setlist in order, with lyric provenance:

```sql
SELECT datetime(ts,'unixepoch','localtime') t, artist, title, source, has_synced
FROM play_events WHERE mode='radio' AND event='play' ORDER BY ts;
```

Average gap between tracks (sanity-check against real track lengths):

```sql
WITH p AS (
  SELECT ts, LAG(ts) OVER (ORDER BY ts) prev
  FROM play_events WHERE mode='radio' AND event='play'
)
SELECT ROUND(AVG(ts-prev)/60.0, 2) avg_minutes FROM p WHERE prev IS NOT NULL;
```

Lyrics coverage and misses:

```sql
SELECT has_synced, COUNT(*) n FROM play_events
WHERE mode='radio' AND event='play' GROUP BY has_synced;

SELECT DISTINCT artist, title FROM play_events
WHERE mode='radio' AND event='no_lyrics';
```

Cache effectiveness:

```sql
SELECT event, COUNT(*) n FROM play_events
WHERE event IN ('cache_hit','cache_miss') GROUP BY event;
```

## Known data-quality issue

Tests write real rows into the production DB — `Test Artist - Test Title`
shows up as the top track with double-digit plays and skews both `top_tracks`
and `top_artists`. Until tests are pointed at a temp DB (or `summarize()`
filters it), discount that row when reporting numbers to the user.
