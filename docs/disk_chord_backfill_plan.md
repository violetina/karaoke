# Disk chord backfill — plan

**Status: not started.** Written 2026-09-21. Waiting on a go-ahead and on the
USB backup disk being mounted.

The short version: ~11.4k tracks in the library have no chord analysis, and the
audio for almost all of them is sitting on the USB backup disk. Analysing them
from disk needs no downloads. The job has not been run yet.

## Why this exists

Post-processing gained an `analyze_harmony` stage (key movement + chord motion).
Before that, `rebuild_vectors` was dead code and chords were only ever produced
by `folder_scan`. With harmony in the chain, the gap became visible: most of the
library has never had chords computed.

The library is ~90% built from disk, so this is deliberately **not** a queue job.
The Celery chain never downloads audio just to analyse harmony — see
`needs_audio` in `download_audio` (`src/karaoke/tasks.py`). Bulk chord work
belongs to `folder_scan`, which reads the drive.

## The numbers (measured 2026-09-21, disk mounted)

| | tracks |
| --- | --- |
| library total | 18,190 |
| playable from disk right now | 16,505 |
| — only on the USB (needs it mounted) | 11,489 |
| — both USB and `~/Music` copies | 4,980 |
| — only off-USB | 36 |
| **missing chords, analysable from disk** | **11,432** |
| — of those, only on the USB | 11,285 |
| missing key/BPM | 518 |
| already complete (chords + analysis) | 5,072 |

Audio files under `Music-backup-DATA`: **20,448**. Of those, 20,265 already have
a `sources` row and 6,018 resolve to a fully-analysed track (skipped with no
decode).

The USB holds the only playable copy for **11,489 of 18,190 tracks**. That is
the bulk of the library, not a stray backup.

## The disk

```
/run/media/tina/52C3-BEF5        932G, 294G used, exfat
```

Scan **only** `Music-backup-DATA`. The other four top-level directories
(`cloudmigration`, `crypt`, `MEEMOO`, `meemoo-old-bak-07-25`) are work/backup
data and must not be ingested into the karaoke library. Walking the whole mount
also takes 10+ minutes on its own.

```
/run/media/tina/52C3-BEF5/Music-backup-DATA
```

## Plan

1. **Confirm the disk is mounted** and `Music-backup-DATA` is readable.
2. **Trial run on one album directory first.** The per-file cost on this exfat
   drive is unmeasured, and 11k decodes is a long job. Use the trial to measure
   decode time per file and confirm the skip ratio on real data.
3. **Full run** over `Music-backup-DATA` once the trial looks right.
4. **Re-check the counts** (query in "Verifying" below) to confirm chords landed.
5. **The 518 missing key/BPM** come along for free — `folder_scan` does key/BPM
   and progression+chords in one decode.

Leave the Celery worker running; it is unrelated to this and only handles the
per-song playback path.

## Skip behaviour (fixed 2026-09-21, needed for this to be safe)

Two holes were closed in `src/karaoke/folder_scan.py`. Both matter here:

1. **The skip was path-based.** It looked up `find_track_by_url(this exact
   path)`, so a USB copy of a song already analysed from `~/Music` would not
   match and would be fully decoded again. There is now a second gate, after the
   tag read and *before* the decode, that resolves artist/title to a `track_id`
   and skips when that track already has CLAP + progression + chords. Costs a
   tag read instead of a decode. This is what keeps the 4,980 dual-copy tracks
   from being re-analysed.
2. **The "already done" sets were capped at 10,000** (`"size": 10000`). CLAP is
   at 5,855 today, but this run pushes it past that, and the *next* scan would
   silently drop tracks from the done-set and re-analyse them. Now scrolled.

So a rerun is cheap and idempotent: already-complete tracks skip without a
decode, whichever copy of the file they arrive from.

## Verifying afterwards

```python
# tracks with chord_motion, library-wide
from karaoke.osclient import client
from karaoke.key_progression import PROGRESSION_INDEX
client().count(index=PROGRESSION_INDEX,
               body={"query": {"exists": {"field": "chord_motion"}}})["count"]
# 5,365 before the backfill
```

## Open items, not blocking

- **~550 playlist tracks still lack chords.** They failed during the
  2026-09-21 playlist run because the USB was not mounted at the time
  (mounted 17:55; the run spanned 17:41–18:19), so the resolver correctly fell
  back to YouTube and the downloaded files were then evicted by the cache.
  339 of them have a local file that exists now and will be picked up by this
  backfill. The remaining ~181 have no local source and genuinely need YouTube.
- **`yt_cache_max_mb` is 500** (`KARAOKE_YT_CACHE_MAX_MB`). Measured sitting at
  497 MiB against the cap, evicting continuously. For the ~181 YouTube-only
  tracks the chain should not assume the file downloaded in `download_audio`
  still exists by the time `analyze_harmony` runs — re-resolve when the path has
  vanished. Not yet done.
- **When the USB is unmounted, 11,489 tracks resolve to YouTube on playback**
  and will download. That is the same fallback that caused the wasted downloads
  above. Worth deciding whether that is wanted.
- **`cache_ingest` still has its own key/BPM implementation**, separate from
  `analyze_audio`. Duplicate logic, not reconciled.

## Uncommitted work

As of 2026-09-21 this is all **uncommitted** on `main`. Do not lose it:

```
M src/karaoke/tasks.py            harmony stage; timings + full flags; download gating
M src/karaoke/postprocess_queue.py  vectors/chords pending; include_search_artefacts
M src/karaoke/postprocess_worker.py run_harmony_logic; fixed run_vectors_logic
M src/karaoke/vector_index.py     iter_track_rows(track_id); new index_track()
M src/karaoke/folder_scan.py      twin-skip gate; scrolled done-sets
M src/karaoke/cache_ingest.py     no timings on bulk ingest
M scripts/enqueue_postprocess.py  fixed SQLite '?' -> Postgres '%s'; db-only steps
M deploy/systemd/karaoke-celery-worker.service   concurrency 2 -> 6
M Makefile                        CONCURRENCY default 2 -> 6
? scripts/enqueue_playlists.py    new: full processing for saved-playlist tracks
+ tests: test_celery_tasks, test_postprocess_queue, test_postprocess_gate, test_tui
```

1778 tests pass, `make lint` clean.
