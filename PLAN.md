# Plan: verified source selection for backfill

Branch `feat/backfill-source-selection`, based on `fix/backfill-postprocess-reliability`.

## The original idea, and what measurement showed

The proposal was to search **YouTube Music** instead of YouTube, on the theory that
it returns more real songs and more official videos. Testing did not support it as
stated, but it pointed at something better.

### YT Music through yt-dlp is a downgrade

`YoutubeMusicSearchURLIE` exists and works, including the songs-only filter
(`&sp=EgWKAQIIAWoKEAoQAxAEEAkQBQ%3D%3D`). What comes back is unusable for matching:

| query | youtube (`ytsearch:`) | ytmusic (`music.youtube.com/search`) |
|---|---|---|
| Kyuss – Apothecaries' Weight | `Apothecaries' Weight` dur=322, ch=`Kyuss - Topic` | `Apothecaries' Weight (Guitar Cover)` dur=None |
| Peggy Lee – Fever | `Peggy Lee - Fever (Official Video)` dur=256 | `Fever` ×4, dur=None, ch=None |
| Nirvana – Heart-Shaped Box | `Nirvana - Heart-Shaped Box` dur=283 | `Heart-Shaped Box`, `None`, dur=None |

No duration, no channel, no artist, sometimes a `None` title — and covers still rank.
Every safeguard added in the preceding branch (Genius slug matching, Spotify
`track_matches`, LRCLIB confirmation for uploader swaps) works by *verifying a
candidate against known facts*. YT Music flat results remove the facts to verify
against, so switching would make selection less safe, not more.

### The addressable population is small

From the last full run (71 gaps):

| outcome | count |
|---|---|
| failed at the lyrics stage, before any video search | 39 |
| reached the YouTube search step | 32 |
| failed after download (Whisper produced nothing) | ~8 |

Lyrics are fetched first and the search only runs for plain-text-only sources, so
**39 of 71 cannot be helped by any search change**. The ceiling here is ~8 gaps.
This is a quality fix, not a hit-rate fix; scope it accordingly.

### What the measurement did find

`backfill_runner.py:175` takes the first result with no verification at all:

```python
yt_results = youtube.search(f"{artist} - {title}", limit=1)
yt_url = yt_results[0]['url']
```

This is the same unverified-match bug already fixed for Genius and Spotify, still
live for video selection. Two concrete failure modes visible in real search output:

- `Peggy Lee - Fever (Full Album)` **dur=4273** — 71 minutes of audio. If picked,
  Whisper transcribes an entire album against one song's lyrics.
- `Kyuss - Apothecaries' Weight (Guitar Cover)` — an instrumental cover, which
  transcribes to nothing. A plausible cause of the ~8 "sync produced no stored
  lyrics" failures.

And the good candidate is *already in the results*, just not deliberately chosen:
`Kyuss - Topic`, dur=322 against LRCLIB's 319.

## The refined approach

YouTube's `- Topic` channels **are** the YouTube Music catalog — auto-generated
official audio, no video intro or outro, which is exactly the "real song, official"
property the original idea was reaching for. They are reachable from ordinary
`ytsearch:` **with durations and channel names intact**, so we get the YT Music
catalog without losing the metadata needed to verify.

Rank instead of switching sources:

1. Widen `youtube.search` from `limit=1` to ~5 candidates.
2. Score each candidate:
   - **+ strong** uploader ends in `- Topic` (official audio)
   - **+** uploader matches the artist (official artist channel, e.g. `Nirvana`)
   - **+** title contains the track name; **−** cover / live / remix / reaction markers
   - **+** duration within tolerance of LRCLIB's `duration` field
3. **Reject** any candidate whose duration is outside tolerance when a reference
   duration is known. This alone kills the 4273s full-album case.
4. Fall back to current behaviour (first result) only when nothing has a reference
   duration, so the change can never do worse than today.

Note the pleasing inversion: earlier in this work `- Topic` was noise to strip out of
*artist* fields. As an *uploader* it is a positive signal for official audio.

## Work items

1. **`youtube.search` returns richer results.** Include `duration`, `uploader`/`channel`
   in the dicts. Currently only `url` and `title` survive, so scoring is impossible.
   Keep the shape backward-compatible — `stage_sources.py` and others call this.
2. **New `select_best_source(candidates, artist, title, duration)`** in
   `backfill_runner.py` (or a small `source_select.py` if it grows). Pure function
   over already-fetched candidates — no network — so it is cheap to unit-test.
3. **Thread LRCLIB's duration through.** `_find_lyrics` currently returns `Lyrics`,
   which has no duration field. Either extend `Lyrics` or return the LRCLIB duration
   alongside. Prefer the smaller change; `fetch_lrclib` already parses the field.
4. **Wire into `_process_gap`**, replacing the blind `yt_results[0]`.
5. **Tests** (all offline, fixtures from the real search output captured above):
   - full-album duration outlier rejected
   - `- Topic` preferred over a higher-ranked cover
   - artist-owned channel preferred over a third-party upload
   - graceful fallback when no duration reference exists
   - existing callers of `youtube.search` unaffected
6. **Re-run** `karaoke-backfill --run --retry-failed` and compare against the
   recorded baseline below.

## Baseline to measure against

State at the end of the preceding branch, for honest before/after comparison:

| status | count |
|---|---|
| processed | 103 |
| failed | 43 |
| invalid | 47 |
| pending | 0 |

Failure reasons on the last full run: 39 × `No lyrics found (LRCLIB or Genius)`,
8 × `sync produced no stored lyrics`. **Only the second group is in scope.** Success
means some of those 8 convert, and no currently-passing gap regresses.

## Explicit non-goals

- Do not switch the search backend to YT Music (measured as worse — see above).
- Do not add `ytmusicapi`. It would give structured results, but it is a new
  dependency, and `- Topic` ranking reaches the same catalog through the existing
  yt-dlp path.
- Do not touch the lyrics-stage failures (39 of 71). Different problem, different fix.
- Do not expect a large hit-rate gain. The honest ceiling is ~8 gaps; the real value
  is not silently aligning lyrics against a 71-minute album or an instrumental cover.
