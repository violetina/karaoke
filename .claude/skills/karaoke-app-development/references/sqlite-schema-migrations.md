# SQLite `ensure_*` schema migrations: run on every connect, must tolerate legacy/minimal schemas

`localcache.connect()` calls every `ensure_*` migration function on open (e.g.
`ensure_gap_columns`, `ensure_play_count_column`, `ensure_silence_*`). These run
against WHATEVER `tracks` table already exists — including bare fixture DBs the
test suite builds and genuinely old databases predating a column.

## The pitfall (bit `ensure_play_count_column`, commit 1043308)

A migration that adds a column and then backfills it by referencing OTHER
columns crashes on connect when those columns don't exist yet:

```python
# WRONG — crashes on a legacy tracks table that has only track_id
conn.execute("ALTER TABLE tracks ADD COLUMN play_count INTEGER NOT NULL DEFAULT 0")
conn.execute("""
    UPDATE tracks SET play_count = COALESCE((
        SELECT COUNT(*) FROM play_events p
        WHERE p.event IN ('play','discover')
          AND LOWER(p.artist) = LOWER(tracks.artist)   -- no such column: tracks.artist
          AND LOWER(p.title)  = LOWER(tracks.title)
    ), 0)
""")
```

Failure surfaced as `sqlite3.OperationalError: no such column: tracks.artist`
during `connect()`, which broke three unrelated tests at once:
`test_alignment_support.py`, `test_cover_store.py`, `test_silence_store.py` —
all `test_*_added_to_a_database_predating_*` cases that create
`CREATE TABLE tracks (track_id INTEGER PRIMARY KEY)` then call `connect()`.

## The rule

Guard every such migration:

```python
have = {r["name"] for r in conn.execute("PRAGMA table_info(tracks)")}
if not have:
    return  # no tracks table yet — the table's own CREATE will carry the column
if "play_count" not in have:
    conn.execute("ALTER TABLE tracks ADD COLUMN play_count INTEGER NOT NULL DEFAULT 0")
    if {"artist", "title"} <= have:   # backfill ONLY when the needed columns exist
        conn.execute("UPDATE tracks SET play_count = COALESCE((...), 0)")
    conn.commit()
```

## Canary

The `test_*_added_to_a_database_predating_*` tests are the tripwire for this
whole class. After adding or editing ANY `ensure_*` migration, run the full
`make test` (or at least those predating-DB tests) — a migration that assumes a
full modern schema will fail there and nowhere else in casual manual testing,
because a real dev DB already has all the columns.

## Play-count denormalisation (the feature that triggered this)

`tracks.play_count` is a denormalised counter driving the "priority: least
played" sort. It is seeded once from the historical `play_events` log by this
migration and kept in step by `record_event` incrementing on `play`/`discover`.
Exposed through `api.list_tracks` -> `TrackResponse.play_count`, sorted in the
TUI via `SORT_OPTIONS` (`least_played` / `most_played`) with a `▶` column.
