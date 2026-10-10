# Live medley recording sync & lyric-alignment tuning

Covers syncing a live recording that contains several songs back-to-back, and
the alignment-quality knobs that decide whether timings land or drift.

## Symptom: chorus drifts tens of seconds late in a long take
A single combined lyric text aligned against a whole multi-minute capture
leaves long stretches with **zero anchored lines** ("anchor drought"). Anything
inside a drought is interpolated (spread by `line_weight`), so a chorus can land
~90s off. Diagnose by dumping the LRC and looking for a big timestamp jump
between two adjacent lines with nothing between them:

```python
from karaoke import localcache, lyrics as L
with localcache.connect() as conn:
    row = conn.execute("SELECT synced_lyrics FROM lyrics WHERE track_id=?", (TID,)).fetchone()
    for t, txt in L.parse_lrc(row["synced_lyrics"]):
        print(f"[{t:7.2f}] {txt[:45]}")
```

## Fix: align each song against its OWN audio slice, then stitch
Do NOT align one medley text against the full recording. Instead, per song:
1. Get per-song time windows from `recording_marks` (query
   `SELECT at_offset, artist, title, ok FROM recording_marks WHERE recording_id=?`).
   Marks with `ok=0` (unidentified — e.g. a spoken intro between songs) bound the
   gaps; overlap windows a few seconds so a song's opening stays inside its slice.
2. Cut the window with `recording_worker.cut(files, start_wall, end_wall, dest_wav)`
   where `files = recording_worker.segment_files(Path(rec['dir']))` and
   `base_wall, _ = recording_worker.recording_span(files)`; `start_wall = base_wall + window_start_s`.
3. Align that song's own stored lyrics against only that slice:
   `words = transcribe_to_words(str(wav), text=plain, language=detect_language(plain))`
   then `align_lines(lyric_lines, words, total_duration=window_len, bpm=bpm, report=report)`.
4. Offset each returned `(t, text)` by the window start and append a `[Song Title]`
   header line; sort all songs' lines by time; `lines_to_lrc(stitched)`; store via
   `localcache.add_track_and_lyrics(artist, title, localcache.Lyrics(synced_raw=lrc,
   plain=..., source="whisper_aligned"), conn=conn)`.

`report["anchored"]` per slice is the quality signal — more anchors = less drift.
A runnable implementation lives at `scripts/sync_live_medley.py` in the repo
(recording 27 / track 960). Whisper on three slices takes minutes: run it
backgrounded with `TQDM_DISABLE=1 TOKENIZERS_PARALLELISM=false`.

## Pitfall: `trust_word()` is a whitelist that drops sung-but-unlisted words
`lyric_align.trust_word(token, prob, lyric_tokens)` keeps a low-confidence
(< `LOW_CONFIDENCE`, 0.15) Whisper word ONLY if it appears in the reference
lyrics (`return token in lyric_tokens`). This is correct for studio tracks (it
kills hallucinations), but on a **live/divergent take** it discards genuinely
sung words that the studio lyrics don't contain, worsening the drought exactly
where the performance improvised. When `lyric_tokens is None` it instead keeps
anything with `prob > 0.005`. So the two real causes of a bad live sync are:
(a) reference lyrics too thin (trimmed medley, one chorus instead of the repeats),
and (b) the whitelist rejecting live-only words. The durable fix is to feed the
FULL live lyrics (all repeated choruses + spoken bits) per song, not to loosen
the whitelist globally (which would let hallucinations back into the studio library).

## Alignment placement uses punctuation as breath weight
`lyric_align.line_weight()` adds pause weight for `, — ; .` so lines with commas
get a slightly longer share during interpolation — natural short breaks. Keep
this in mind when a line seems to hold too long/short.

## LRC formatting: always a space after the closing bracket
`caption_sync.json3_to_lrc` / `json3_to_enhanced_lrc` must emit
`[mm:ss.xx] text` (space after `]`), not `[mm:ss.xx]text`. Whisper's
`lines_to_lrc` already does. Stored DB rows are already spaced — verify with a
`synced_lyrics LIKE` scan before mass-fixing.

## Sourceless rows after add_track_and_lyrics
`add_track_and_lyrics` / recording ingest create a `tracks` row with NO `sources`
row. Resolve with `youtube.search(f"{artist} - {title}", limit=1)` then
`INSERT INTO sources (track_id, kind, url) VALUES (?, 'youtube', ?)`. The
`sources` table has columns `source_id, track_id, kind, url, player_name` (no
`added_at`); `sources.url` is UNIQUE, so a duplicate video ID raises — fall back
to a `youtube_search` results-page URL. Find sourceless rows with a
`LEFT JOIN sources ... WHERE s.source_id IS NULL`.
