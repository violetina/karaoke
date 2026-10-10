# Transcribe-path duplicate tracks & Whisper limits on remixes

Two recurring gotchas discovered while adding word-level timing to already-cached
tracks. Both bite when you try to "upgrade" a song that ALREADY exists in SQLite.

## 1. The transcribe path creates duplicate tracks (recurring bug)

`karaoke --youtube <url> --download --force-transcribe` and
`karaoke -f <file> --force-transcribe` **insert a brand-new track** from
parsed-title / filename metadata instead of updating the existing matched track.
Result: one duplicate per run.

Observed this session on track 219 `"Black Out Days (Stay Away)"`:
- `--youtube … --force-transcribe` → spawned 364 `"Ian Asher & Phantogram- Black Out Days"`.
- `-f <webm> --force-transcribe --lyrics-file …` → spawned 365 with
  `artist='' title='FFxsTiFGWT8'` — because the cache webm has no tags and
  `identify.from_file` falls back to the video-id filename.

### Mitigation (until fixed in code)
- After any transcribe run, remove the spurious rows:
  ```sql
  DELETE FROM lyrics  WHERE track_id=<dupe>;
  DELETE FROM sources WHERE track_id=<dupe>;
  DELETE FROM tracks  WHERE track_id=<dupe>;
  ```
  or just run `make db-cleanup`.
- To pre-empt the tag-less-filename dupe, resolve canonical metadata first:
  `youtube.resolve_youtube(url)` returns clean `artist`/`title` even for an
  untagged webm, whereas `identify.from_file(path)` returns
  `artist='' title='<video-id>'`.

### Root-cause fix (recommended PR)
Make the transcribe flow call `find_track_id` / video-ID matching (see
`localcache.find_track_by_url` + `extract_youtube_id`) and UPDATE the matched
track's lyrics in place, rather than always inserting.

## 2. Whisper is unreliable on music-heavy audio and remixes

- **Raw `--force-transcribe` ASR hallucinates** on dense production: invented
  lines and foreign-script garbage (e.g. `D.O. Hall`, `リ辕话`) appeared for
  "Black Out Days".
- **`--lyrics-file` alignment** (feed known-good LRCLIB plain text so Whisper only
  aligns timing) fixes the WORDS but **not the TIMING for remixes/edits**. An Ian
  Asher edit chops, pitches, loops and rearranges the vocal, so the original
  verse/chorus lyric structure no longer maps onto the audio timeline; alignment
  stays messy and incomplete.

### Decision rule
- For **remixes/edits**: keep the existing LRCLIB **line-level** timing — it is
  the most accurate option available. Do not overwrite it with a worse Whisper run.
- Pursue Whisper **word-timing** only for tracks with clean, faithfully-structured
  vocals, or prefer real **json3 captions** (the post-processing `timings` step)
  when the source video has them.
- Never present a failed multi-attempt Whisper run as a "working" result; revert
  and report honestly.
