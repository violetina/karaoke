# Audio vs video lyrics sync

A track can play as its audio upload or as its music video. The video often has an intro,
so its lyrics need a per-track offset. That offset is stored in `track_sync_offsets` with
`mode='video'` and is found by comparing the two audio streams (waveform
cross-correlation). On the newest 200 tracks (2026-11-28) this synced 75 of 105 candidates
in 9 minutes with 0 errors; 53 offsets were within 0.5 s.

Code: `src/karaoke/caption_sync.py` (`sync_audio_video_counterparts`,
`record_av_sync_result`), `src/karaoke/av_align.py` (the cross-correlation),
`scripts/batch_sync_audio_video.py` (batch). The original design is archived in
[A/V waveform sync plan](archive/av-sync-plan.md).

## How it works

`sync_audio_video_counterparts(track_id)` tries three methods, in this order:

1. **Waveform.** Download the audio upload and the music video, cross-correlate the two
   audio streams and store the offset. Gives a confidence. Needs two downloads.
2. **YouTube captions.** If the video has real captions, align the audio lyrics against
   them as text.
3. **Whisper anchor** (opt-in, `--enable-whisper-fallback`). Transcribe the video's audio
   and use it as a coarse anchor. Off by default: slow, CPU-bound and not fully
   deterministic.

When an offset exists and the track has no real YouTube captions, a **derived video lyrics
row** is stored: the audio lyrics shifted by the offset, with source `derived_from_audio`
(`derived_from_audio_whisper_anchor` for the Whisper path). A derived row is never treated
as real captions, so it is regenerated whenever the offset changes. Before PR #164 it was,
and derived rows went stale: 7 of 13 were out of date before the fix, 0 after.

A stored non-zero offset is trusted: the waveform step is skipped for it unless
`--force-waveform` is given. A stored `0.0` is re-checked. Of 1469 video offsets, 1225 were
exactly `0.0`, believed to be artifacts of an old bug that aligned the audio lyrics
against themselves.

## Reading lyrics: two conventions, never mix

| reader | loads | offset |
|---|---|---|
| stage (`stage_view.py`) | the lyrics row for the current mode (`media_type='audio'` or `'video'`) | none; the video row is already shifted |
| TUI | the audio lyrics | adds the stored `mode='video'` offset itself |

`localcache.get_lyrics_by_track_id(track_id, conn, media_type=None)` returns the **audio**
row (PR #162). Before, it returned the newest row, which for 14 tracks was the shifted
video copy. That affected the library view, the mood-still timeline, the lyric-vibe MCP
tool, sentiment and TUI/CLI fetch, and the TUI shifted twice in video mode.

## The guard

A waveform result is rejected when all of these hold (PR #164):

- a non-zero video offset is already stored,
- the new offset is more than 5 s away from it (`GUARD_MAX_JUMP_S`),
- its confidence is below 0.3 (`GUARD_MIN_CONFIDENCE`).

The stored value is kept and the result is logged with method `kept_stored`; the
rejected value is logged at INFO. Such tracks are not marked as waveform-verified, so they
are re-checked on later batch runs. The guard only protects existing values: a first-time
offset is stored whatever its confidence.

## Running the batch

```bash
make batch-sync-av                                  # 50 tracks not yet waveform-verified, 4 workers
make batch-sync-av LIMIT=200 WORKERS=4
make batch-sync-av TRACK_ID=20301 FORCE_WAVEFORM=1  # re-check one track, even with a stored offset
make batch-sync-av FORCE=1 LIMIT=100                # also tracks already checked

# several specific tracks, with browser cookies
.venv/bin/python scripts/batch_sync_audio_video.py --track-id 101 --track-id 102 \
  --force-waveform --cookies-from-browser chrome
```

| option | effect |
|---|---|
| `LIMIT=N` / `--limit` | max tracks (default 50) |
| `WORKERS=N` / `--workers` | worker threads (default 4) |
| `FORCE=1` / `--force` | include tracks `av_sync_log` already marks as waveform-verified |
| `TRACK_ID=N` / `--track-id` | only this track; the script accepts it repeatedly |
| `FORCE_WAVEFORM=1` / `--force-waveform` | re-run the waveform even when a non-zero offset is stored |
| `--cookies-from-browser NAME` | pass browser cookies to yt-dlp |
| `--enable-whisper-fallback` | opt in to the Whisper anchor |

Candidates are tracks with synced lyrics and a YouTube source, listed once per track
(PR #163). Keep yt-dlp current (`.venv/bin/pip install -U yt-dlp`); browser cookies help,
YouTube answers with 429s otherwise. With 4 workers a track takes about 5 s; for long runs
use `nice -n 10`.

## Reading `av_sync_log`

Every sync path writes one row per track since PR #162 (before, only the batch did, so all
1497 rows had method `NULL`).

| column | values |
|---|---|
| `track_id` | primary key |
| `status` | `synced`, `unaligned`, `no_captions`, `no_audio_lyrics` |
| `offset_s` | seconds; `NULL` unless `synced` |
| `method` | `waveform`, `captions`, `whisper`, `cached`, `kept_stored` |
| `confidence` | waveform confidence (waveform and `kept_stored` only) |
| `checked_at` | last check |

Counts per status and method:

```bash
PYTHONPATH=src .venv/bin/python -c "
from karaoke import localcache
cur = localcache.connect().cursor()
cur.execute('SELECT status, method, count(*) AS n FROM av_sync_log GROUP BY 1, 2 ORDER BY 3 DESC')
for r in cur.fetchall(): print(r['status'], r['method'], r['n'])
"
```

Big offsets with low confidence, worth checking by ear:

```bash
PYTHONPATH=src .venv/bin/python -c "
from karaoke import localcache
cur = localcache.connect().cursor()
cur.execute('''SELECT a.track_id, t.artist, t.title, a.offset_s, a.confidence, a.method
               FROM av_sync_log a JOIN tracks t USING (track_id)
               WHERE abs(a.offset_s) > 5 AND coalesce(a.confidence, 0) < 0.3
               ORDER BY abs(a.offset_s) DESC LIMIT 20''')
for r in cur.fetchall():
    print(r['track_id'], r['artist'], '-', r['title'], r['offset_s'], r['confidence'], r['method'])
"
```

## Results (2026-11-28)

### Pilot: 14 tracks with a shifted video row

The 14 tracks that had both an audio and a shifted video lyrics row, run with
`--force-waveform` and 2 workers: 0 errors.

| outcome | tracks |
|---|---|
| confirmed, unchanged | PJ Harvey - Man-Size +3.72 s; Balthazar - Bunker -4.04 s (the old caption method had stored a spurious -18.08 s earlier); Tom Waits - Army Ants; Feint - Vagrant; Dennis Lloyd - Nevermind; Frøislie - Demring -1.83 s; Prince - Purple Rain +72.42 s; New Order - Ecstacy; Tzanca - Uraganu |
| corrected | Bauhaus - Ziggy Stardust +4.5 → +2.39 s (confidence 0.52); Queen - Don't Stop Me Now -1.14 → +2.85 s (0.40) |
| rejected by the guard | Joy Division - Shadowplay (2007 remaster): waveform said +14.17 s at 0.20, probably a different performance in the video; kept -1.95 s |
| fell back to captions/cached | Gary Numan - Cars; New Order - Blue Monday |

For the last two, YouTube Music search returned a different audio upload on repeated
calls (2 of 14 tracks non-deterministic).

### Newest 200 tracks

Track IDs 20274-20473. There is no date-added column; `track_id` is assigned in insertion
order. OpenSearch `tracks.indexed_at` only follows insertion order for the newest ~80
tracks, because of a bulk reindex on 2026-11-18/19.

105 of the 200 were candidates (synced lyrics and a YouTube source). Run with
`--force-waveform`, 4 workers, `nice 10`: 9 minutes, 0 errors.

| status | tracks |
|---|---|
| synced (waveform) | 75 |
| unaligned | 21 |
| no captions/video | 9 |

| offset | tracks |
|---|---|
| within 0.5 s | 53 |
| 0.5-5 s | 13 |
| above 5 s | 9 |

The 3 tracks that already had an offset got the identical value. Stale derived rows: 0.

Worth checking by ear (big offset, low confidence, no stored value for the guard to
protect):

| track | offset | confidence |
|---|---|---|
| Timal - Sicario | +34.43 s | 0.22 |
| K/DA - FIRESTARTER | +11.45 s | 0.23 |
| Dirty Heads - Dance All Night | +8.24 s | 0.25 |

Big offsets with solid confidence (long video intros):

| track | offset | confidence |
|---|---|---|
| Lewis Capaldi - Someone You Loved | +6.25 s | 0.69 |
| Ed Sheeran - Perfect | +18.44 s | 0.43 |
| Tiakola & Jorja Smith - Jet Lag | +8.13 s | 0.46 |
| Franglish & KeBlack - Generation impolie | +31.88 s | 0.42 |

### Throughput

About 5 s per track with 4 workers. A full library re-check (8883 candidates) would take
about 12-13 hours; it has not been run yet.

## Open issues

- **No confidence floor for first-time offsets.** The guard only protects existing values,
  so a weak first match (Timal - Sicario, 0.22) is stored as is.
- **`kept_stored` tracks re-download on every batch run**, because they are never marked
  as waveform-verified.
- **YouTube Music search is non-deterministic**: it can return a different audio upload on
  repeated calls, which changes or breaks the waveform match (2 of 14 in the pilot).
