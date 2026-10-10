# DB Gaps, Source Resolution & Self-Healing

The `tracks`/`sources`/`lyrics` split creates predictable data gaps because
Spotify, YouTube, and lyric providers (LRCLIB/Genius) format metadata
differently. These recur — build tooling to heal them rather than fixing by hand.

## The three gap classes
1. **Duplicate tracks** — same song, disjoint records. e.g. Spotify reports
   artist `"Inpatient"`; staging saves canonical `"Inpatient, Ren & Chris Webby"`.
   The Spotify source and the approved lyrics end up on *different* track_ids, so
   the song plays with no lyrics in one mode but shows lyrics in another.
   Also: `"Balthazar"` vs `"Balthazar - Topic"`, `"Captain Beefheart"` vs
   `"Captain Beefheart & His Magic Band"`.
2. **Orphan tracks** — approved lyrics but NO row in `sources` (un-openable on
   Enter; `url`/`kind` come back NULL).
3. **Orphan cache files** — a `*.webm` sits in `~/.local/share/karaoke/youtube/`
   with no `tracks`/`sources` mapping (never indexed, or the record was merged/deleted).

## Self-healing script: `scripts/db_cleanup.py` (`make db-cleanup`)
Runs three phases, all best-effort and idempotent:
- **Dedup**: group tracks by lowercase title; merge when artists are
  "compatible" (substring, shared first word ≥3 chars, or matching after
  stripping `- topic`/`official`/punctuation). Consolidate sources, lyrics
  (synced beats plain), and `track_analysis` (row with BPM wins) into the
  canonical record (prefer the one WITH lyrics, else the longer artist string),
  then delete the duplicate. Also prunes mock/test rows (titles `analyse`,`tui`).
- **Source healing**: for tracks with lyrics but no source, `youtube.search(query, 1)`
  and insert the webpage URL. Throttle ~1s; cap per run (e.g. 30).
- **Orphan cache healing**: for each `*.webm` not in `sources`, `fetch_metadata`
  → `parse_youtube_title` → create/reuse track + source → `analyze_audio` →
  `track_analysis.save_detected` (key/BPM/energy/brightness).

## Source resolution pitfall — browse/TUI opens the WRONG source
`LEFT JOIN sources s ON t.track_id = s.track_id ... GROUP BY t.track_id` returns
an **arbitrary** source per track. After dedup, many tracks have BOTH a Spotify
and a YouTube source; SQLite often returns the Spotify row. `open_song_url` then
routes `kind='spotify'` through `playerctl open` (needs the Spotify desktop app
as active MPRIS target) instead of opening the YouTube page in the browser — so
Enter silently does nothing. **Fix**: deterministically pick a browser-openable
source with a correlated subquery, priority youtube > http > spotify > other:

```sql
LEFT JOIN sources s ON s.source_id = (
    SELECT s2.source_id FROM sources s2
    WHERE s2.track_id = t.track_id
    ORDER BY CASE
        WHEN s2.kind = 'youtube' THEN 0
        WHEN s2.url LIKE 'http%'  THEN 1
        WHEN s2.kind = 'spotify' THEN 2
        ELSE 3 END, s2.source_id
    LIMIT 1)
```
Apply this to EVERY place that lists songs for opening (both `browse.py` and
`tui._load_tracks`) — fixing one leaves the other broken.

## Scan-mode lyric miss — empty artist from browser MPRIS
YouTube Music tabs frequently expose `xesam:artist=""` and only a title
(`"Cup Runneth Over | YouTube Music"` → cleaned to `"Cup Runneth Over"`). Exact
`(artist,title)` lookup fails because the DB has the real artist. `resolve_lyrics`
needs a title-only fallback when artist is blank AND there's no URL match: match
`lower(title)` and prefer a row that already has synced lyrics.
