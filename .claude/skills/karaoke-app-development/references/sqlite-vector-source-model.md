# Karaoke session lessons: SQLite, browse, backfill, vector search

Use this note when working on the local `karaoke` app after the SQLite data-model refactor.

## Durable design decision

SQLite is the operational source of truth for playback and automation:

- `tracks`: canonical artist/title identity.
- `sources`: YouTube URLs, Spotify URIs, player/local source references.
- `lyrics`: approved/staged/rejected plain and synced lyrics.
- `play_events`: radio/player/listen/query stats.
- `lyric_gaps`: missing-lyrics queue for backfill.

OpenSearch should be treated as optional and derived. Rebuild it from SQLite/local files for semantic search, line vectors, sentiment and timing-training experiments. Do not make normal playback depend on OpenSearch availability.

## Debugging browse/player source failures

If `make browse` shows songs but Enter appears to do nothing, inspect SQLite first. The most common cause is missing `sources.url`, especially for older radio/history rows that only have artist/title.

Useful inspection query:

```sql
SELECT t.track_id, t.artist, t.title, s.kind, s.url
FROM tracks t
LEFT JOIN sources s ON t.track_id = s.track_id
ORDER BY t.artist, t.title;
```

If no URL is stored, a reasonable fallback for browse is to open a YouTube search for artist+title rather than silently returning.

## YouTube module caution

`src/karaoke/youtube.py` is a mature module. Preserve title parsing, cookie options, cache summary/prune/clear, `fetch_metadata`, and `resolve_youtube`. If backfill needs `search()` or `download()` helpers, add wrappers around the existing functions rather than replacing the module.

## Working pattern after cascading regressions

When several patches start breaking adjacent features:

1. Stop adding speculative fixes.
2. Inspect `git status`, the touched files, and git history.
3. Restore the last known-good mature module from history if needed.
4. Re-apply only the minimal intended change.
5. Run focused compile/tests and then full `python -m pytest` for broad data-flow changes.
6. Show the DB rows or command output before claiming behavior is fixed.

## Recovering orphaned songs from legacy OpenSearch generations

When SQLite became the source of truth, songs indexed by *earlier* pipelines were
left stranded in OpenSearch only. Check for multiple `source` generations before
assuming the index is merely "stale":

```bash
curl -s localhost:9200/tracks/_search?size=0 -H 'Content-Type: application/json' \
  -d '{"aggs":{"s":{"terms":{"field":"source","size":20}}}}'
```

Observed generations: `sqlite` (current, `karaoke-vector-index`), `lrclib-cache`,
`spotify` (Spotify import), `local` (legacy `music-index` scanner). A doc count
*higher* than `SELECT COUNT(*) FROM tracks` means orphans, not staleness — 103
songs (97 already carrying lyrics) were recovered this way.

Recovery: read the non-`sqlite` docs, skip any already in SQLite by
`(lower(artist), lower(title))`, then `add_track_source(..., kind='legacy')` +
`put_cached_lyrics(...)` reusing the doc's `synced_lyrics`/`plain_lyrics` so
lyrics are not re-fetched. Only then delete the legacy docs and rebuild:

```bash
# _id does NOT support prefix queries (HTTP 400); filter on `source` instead.
curl -XPOST 'localhost:9200/tracks/_delete_by_query?refresh=true' \
  -H 'Content-Type: application/json' \
  -d '{"query":{"bool":{"must_not":[{"term":{"source":"sqlite"}}]}}}'
karaoke-vector-index --rebuild --lines
```

### Pitfalls
- **Back up first**: `cp ~/.local/share/karaoke/karaoke.db /tmp/karaoke.db.bak-$(date +%s)`.
- **Skip test fixtures.** `~/Music/karaoke-samples/*.mp3` are 1-second stubs tagged
  with real song names (Queen, Nirvana, R.E.M.). They leak a bogus
  `duration≈1.045` into real tracks, which then poisons LRCLIB duration matching.
  Filter them on import and null implausible values:
  `UPDATE tracks SET duration=NULL WHERE duration IS NOT NULL AND duration < 30;`
- **`music-index` writes to OpenSearch only** — it does not populate SQLite. To add
  local audio to the library use `extract_tags()` +
  `add_track_source(..., kind='local')`, then `fetch_lrclib()`.
- `lyric_gaps` rows recorded from MPRIS browser metadata carry malformed
  artist/title (e.g. `"Fugazi" / "Topic - Waiting Room"`), so they never match
  cleaned track rows and stay `pending`. Clean the names before matching.

## Bulk lyric backfill via LRCLIB

For tracks with no lyrics, `fetch_lrclib(artist, title, album, duration)` +
`localcache.put_cached_lyrics(...)` is far cheaper than the Whisper path. Always
dry-run first to measure the hit rate; expect roughly 60% (33/55 observed, 30 of
them synced). Sleep ~0.34s between calls to stay polite to LRCLIB. Misses are
usually instrumentals or obscure releases — those still need the Whisper flow.

Titles carrying YouTube artifacts (`"Artist - Title [Official Music Video]"`,
track-number prefixes like `"11. Gramatik"`) cause both LRCLIB misses and
duplicate rows. Clean them, then re-fetch. Plain-only lyrics sourced from YouTube
captions generally cannot be upgraded via LRCLIB (0/32 observed) — use Whisper.

## YouTube captions as a synced-lyrics source (no Whisper)

yt-dlp exposes two caption sets, mirroring YouTube's caption options:
`subtitles` (uploader-supplied — often the real lyrics) and
`automatic_captions` (ASR). Request either as **`json3`** and you get per-word
millisecond offsets:

```python
{"tStartMs": 21920, "segs": [{"utf8": "Heat"}, {"tOffsetMs": 80, "utf8": " up here."}]}
```

`karaoke.caption_sync.probe_captions(info)` reports availability and picks the
best track (manual > automatic, json3 > vtt/srv3); `json3_to_lrc(payload,
max_words=10)` emits LRC. CLI: `karaoke-stage captions <url>` detects,
`karaoke-stage youtube <url>` stages synced lyrics when json3 exists.

Yield on a real library: of 25 tracks with a YouTube source but no synced
lyrics, 9 gave usable captions (>=8 lines), 3 weak, 12 none.

### Caption pitfalls (all observed on live videos, not theoretical)
- **`live_chat` is not a caption track** but appears under `subtitles`. Also
  filter pseudo-languages like `en-ehkg1hFWq8A` (a video-id suffix); only
  accept real codes (`en`, `en-orig`, `en-GB`, `nl`).
- **Sound tokens appear mid-line**, not just standalone: `"[music] Heat. Heat."`.
  Strip `[music]`/`[Applause]`/etc. anywhere in the text, then drop cues that
  become empty.
- **`>>` speaker markers** prefix auto-caption cues.
- **HTTP 429 returns an HTML page.** Check `Content-Type`/leading `<` before
  parsing, otherwise a rate limit silently looks like "captions were empty".
  Bulk probing *will* trigger it — throttle, or use `--cookies-from-browser`.
- **Caption URLs expire quickly.** Re-run `extract_info` rather than reusing a
  URL captured minutes earlier.
- **Manual captions run long** (~14 words/line vs LRCLIB's 5-9). Split on the
  per-word `tOffsetMs` values so line starts are genuine word times; never
  interpolate.
- Auto-caption *text* can be badly wrong even when timing is right (Tom Waits
  came back as "you can't hurt me like a cat like up"). Always stage for review
  rather than auto-approving.

## Word-highlight timing and instrumental gaps

LRCLIB gives **per-line** timestamps only, so the renderer interpolates word
positions across a line's duration. `LyricTimeline.active_fraction` originally
assumed a line lasts until the *next* line's timestamp, which meant an
instrumental break stretched the word highlight across the whole break (issue
#21): on `Ren - Hi Ren` (51.7s gap) it advanced ~one word per five seconds.

Fix: bound a line's duration (`LyricTimeline.line_end`). **Calibrate the pace
per song** from its own normal lines (median s/word) — measured across the
local cache, real songs range 0.55 s/word (Tom Waits) to 1.16 s/word, so a
hard-coded constant cuts slow songs short. Clamp with `MIN_LINE_S`/`MAX_LINE_S`.

Measuring the blast radius before merging is cheap and worth it — sweep every
synced track and count corrections:

```python
lines, ends, words = parse_enhanced_lrc(row["synced_lyrics"])
tl = LyricTimeline(lines, ends=ends, word_times=words)
for i in range(len(lines) - 1):
    gap = lines[i + 1][0] - lines[i][0]
    if gap >= 6.0:
        old, new = gap, tl.line_end(i) - lines[i][0]
```

That sweep found 948 long gaps across 160 tracks, 789 corrected (worst:
Motorpsycho "Vortex Surfer" 172.2s -> 7.0s).

### Enhanced LRC is the standard for word-level timing

```
[00:21.92]<00:21.92>Heat <00:22.00>up <00:22.22>here.
```

Supported by AIMP, foobar2000/ESLyric, Kugou, QQ Music, NetEase. Use
`parse_enhanced_lrc()` -> `(lines, ends, word_times)`; `parse_lrc()` remains a
thin wrapper with its original signature so existing callers are unaffected.

**YouTube json3 captions already carry per-word `tOffsetMs`**, so
`json3_to_enhanced_lrc()` gives word-accurate highlighting for caption-sourced
tracks with no manual tapping at all. For tracks approved *before* Enhanced LRC
support, `karaoke-upgrade-timings` (`make upgrade-timings`) backfills word
timings in place — it skips tracks that already have word tags and preserves the
existing plain lyrics, since captions supply timing while the approved plain
text may be a better LRCLIB transcription.

### YouTube caption rate limiting (HTTP 429)
Bulk caption work gets throttled aggressively and the limit **persists for a
long time** (observed well over an hour). Critically, the two endpoints fail
independently: `extract_info` metadata calls keep succeeding while the
`timedtext` caption download returns 429 with an **HTML error page**, so any
bulk tool must check `Content-Type`/leading `<` or a rate limit reads as
"captions were empty". Throttle (4-6s between fetches), stop the whole run on
the first 429 instead of looping through every remaining track, and offer
`--cookies-from-browser`. Prefer `--dry-run` to enumerate candidates without
spending requests.

### Pitfalls
- `parse_lrc` used to **silently drop empty-text timestamps** (`[00:14.00]`
  alone), which is the conventional LRC way to say "this line ends here".
  Anything relying on end markers needs the enhanced parser.
- `LyricTimeline.lines` is `list[tuple[float, str]]` and is indexed by a lot of
  code and tests. Add `ends`/`word_times` as **parallel dicts** keyed by line
  index rather than widening that tuple.
- Existing tests can encode the old stretch behaviour. `test_active_fraction_midline`
  uses 1-word lines spaced 10s apart — a fixed per-word cap breaks it. That test
  is a useful canary for "does this change hurt slow songs?".
- A gap threshold (`GAP_MIN_S`, 6s) is needed before showing a rest marker, or
  it flickers between ordinary lines.

## Docs to update when schema/source-of-truth changes

- `docs/local-cache-and-stats.md`
- `docs/cache-schema.md`
- `docs/index.md`
- `README.md`
- vector/search plans under `docs/`
