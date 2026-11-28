> [!note] Archived: implemented, kept for history
> Shipped in PR #146 and #147 (waveform A/V sync, toggle, Whisper fallback) and the
> follow-ups in `fix/av-sync-follow-ups`. Lyrics convention since then: the stage loads
> the mode-specific lyrics row and applies no offset; the TUI loads the audio lyrics and
> adds the per-clock offset from `track_sync_offsets`. Never mix the two.
> Current docs: [Audio vs video lyrics sync](../av-sync.md).

# Plan: audio vs video toggle and waveform-based A/V sync

Written 2026-10-30 so another session can pick it up. `PLAN.md` in this directory belongs to a different
effort (backfill source selection), so this plan has its own file.

Worktree and branch already created, nothing committed yet: `~/karaoke-av` on `feat/av-waveform-sync` (from `main` at `5b3197c`).
Work there, not in `~/karaoke`: other agents share that checkout and the services run from it
(see memory `other-agents-share-the-checkout`).

## Ask (Tina)

1. Toggle audio vs video (YouTube Music Song/Video).
2. Check the sync difference between the two versions. A flow existed once, but it was not structural.
3. Test on songs that have both versions, and integrate it into the full processing flow: detect two versions, calculate the diff,
   fall back to a Whisper resync as the last resort. Idea from Tina: comparing the two waveforms might make drift easy to detect.

## What already exists (verified 2026-10-30)

| Piece | Where | State |
|---|---|---|
| Toggle | `player_open.cdp_toggle_av(mode)` (`player_open.py:414`), TUI key `V` (`tui.py:4342`) | Set or blind-flip only. **No read of the current mode**, and **not exposed** in `ctrl_api`, the MCP server or the dashboard. Returns `no_av_toggle` when the track has no counterpart |
| Version lookup | `ytmusic_client.get_audio_and_video_versions(artist, title)` | Returns `audio_video_id` + `video_video_id`. The video id falls back to `search_track(filter_type="videos")`, which can be a live/lyric video, i.e. a different master |
| Caption offset | `lyrics.align_audio_and_video_lyrics` + `caption_sync.sync_audio_video_counterparts` (`caption_sync.py:362`) | Fuzzy-matches lyric lines and takes the median delta (video minus audio). Needs synced lyrics on both sides, so it fails on songs without captions |
| Offset store | `track_sync_offsets(track_id, offset_s, updated_at, mode)`; `mode='video'` is the video-vs-audio offset | See "Data" below |
| Batch + wiring | `scripts/batch_sync_audio_video.py` (`make` target at `Makefile:281`), `postprocess_worker.py:175` and `:358`, `batch_enrich_chords_and_vectors.py:278` | Already runs in the postprocess flow, but only the caption method |
| Tools | `~/karaoke/.venv`: numpy, scipy, librosa, soundfile, yt_dlp, ytmusicapi, faster_whisper; `.venv/bin/yt-dlp`; `/usr/bin/ffmpeg` | Use that venv. The default shell PATH points at the platform-engineering venv, which has none of them |
| Aligner | `lyric_align.py`, `scripts/eval_align.py` | Whisper-based; see memory `measure-alignment-against-known-lyrics` |

Memories to read first: `ytmusic-video-mode-shifts-sync`, `sync-offsets-are-per-clock`, `measure-alignment-against-known-lyrics`,
`other-agents-share-the-checkout`.

## Data (read-only look at the DB, 2026-10-30)

- `track_sync_offsets mode='video'`: 1463 rows; **1236 are exactly 0.0**, 226 have |offset| > 0.5 s, range -144.37 to +4.5 s.
  The 0.0 rows are probably "nothing found" defaults and the -144 s looks like an outlier. **The provenance of these offsets is unverified.**
  Do not treat them as ground truth until it is known whether they came from captions, from a manual nudge, or from a default.
- `lyrics`: 10,930 audio rows and **1** video row, so the caption path has stored almost no video lyrics.
- `av_sync_log`: 734 rows, all `synced`. `ensure_log_table` seeds it from every `track_sync_offsets` row of any mode, and
  `find_candidates` skips any track that has a row. So those 734 do not prove the A/V flow ran on them.

## Design

### 1. Toggle (small)
- Add `cdp_get_av()` that only reads the active state (`song-button` `selected` / `aria-selected`) and returns `audio` | `video` | `no_av_toggle`.
- Expose get/set on `ctrl_api` and as an MCP tool. Testing it live changes what the kiosk plays: pick a moment Tina is fine with,
  and never restart `karaoke-kiosk.service` (memory `feedback_karaoke_kiosk_patience` in the meemoo project).

### 2. Aligner `src/karaoke/av_align.py`
- Inputs: two audio files (audio release and video edit, `bestaudio` via yt-dlp, mono 22.05 kHz).
- Features: gain-invariant onset envelope or chroma at 20-40 ms hops. Not raw waveforms: the two versions differ in codec, loudness and mastering.
- Coarse global lag by FFT cross-correlation over a wide range (cold opens can be long), then lag per 20-30 s window in a narrow band
  around it, each with a confidence (peak versus runner-up).
- Classify: constant offset, step (an edit or cut), linear slope (speed change), or "not the same recording".
  Low confidence everywhere returns `None` and falls through. Store nothing in that case.
- Only add `numpy`/`scipy`/`librosa` use if `requirements-audio.txt` / the `beats` extra already declares them (librosa does; check scipy). No new dependencies.
- **Sign convention:** `mode='video'` means video time minus audio time, as in `align_audio_and_video_lyrics`. A flipped sign shifts every lyric the wrong way.

### 3. Validate before integrating
1. Synthetic test first (no network): take a local file, prepend N seconds of other audio, add a small time stretch, re-encode lossy, change the gain.
   Recover N and the slope, and assert the sign.
2. Real pairs: 5 to 10 tracks where `get_audio_and_video_versions` returns two distinct ids. Compare waveform offset with the caption offset and,
   where it exists, with a hand-nudged value. Cases where they disagree are the interesting ones.
3. Score with the method from `measure-alignment-against-known-lyrics`: signed median, jitter, marker spread, read separately.

### 4. Integrate into `sync_audio_video_counterparts`
Order of preference:
1. Two ids known: waveform offset (this plan), stored with `set_sync_offset(..., mode="video")`. The existing "derive video lyrics from audio lyrics" path then works unchanged.
2. Otherwise the existing caption path.
3. Otherwise faster-whisper as a coarse anchor only (fixed temperature 0; Whisper is not deterministic, tens of seconds of jitter across runs).
- Add `method` and `confidence` columns to `av_sync_log`, and make `find_candidates` skip only rows written by the new method, so the old caption-only rows get re-evaluated.
- Persist per-segment offsets only if the real pairs show non-constant lag. A scalar is enough otherwise.
- Keep downloads in the postprocess queue, not inline in the API.

## Risks and constraints
- A wrong master (live/lyric video) cannot be fixed by any offset: refuse when confidence is low, and log it.
- The answer key is uncertain (see Data). Agreement between waveform and caption is the check; do not tune against the 0.0 rows.
- Do not refactor `caption_sync`. Do not restart services or add Hermes cron jobs without asking (both karaoke jobs are paused, memory `karaoke-jobs-run-under-hermes`).
- Commit on `feat/av-waveform-sync` in `~/karaoke-av`; open a PR, no direct push to `main`.

## Status
- [x] Orientation, existing code and data read, advisor consulted
- [x] Worktree `~/karaoke-av` on `feat/av-waveform-sync`
- [x] Toggle read + expose (commit `ca155ce` on `feat/av-waveform-sync`: `cdp_get_av()`, fixed a live blind-toggle
      bug that always landed on audio, `GET/POST /api/players/av`, MCP tools `toggle_audio_video`/`get_av_mode`)
- [x] `av_align.py` + synthetic test (commit `d5aaa77`: onset-envelope cross-correlation, coarse-prefix +
      per-window confidence-weighted fit, sign pinned empirically; 4 new synthetic tests, all pass)
- [x] Real-pair test (5 to 10 tracks) and comparison with the caption offsets (commit `737f149`:
      `scripts/eval_av_align_real_pairs.py`, validated on P.J. Harvey "Man-Size", Queen "Don't Stop Me Now",
      Balthazar "Bunker" — all user-suggested, confirmed to have 2 distinct ids. Found the interesting
      disagreement the plan called for: Bunker's caption offset (-18.08s) looks like a spurious text-match,
      the waveform method's -4.04s has a ~14-22x stronger correlation peak. Fixed a real bug this surfaced:
      a single fixed coarse-prefix length can't serve both a quiet-intro real track and a synthetic speed
      change, so the coarse pass now tries several prefix lengths and keeps the highest-confidence one.
      Also found: get_audio_and_video_versions isn't fully deterministic across calls for at least one
      track — flagged for the integration step, not yet resolved.)
- [x] Integrate into `sync_audio_video_counterparts` + `av_sync_log` columns (commit `9486bc7`: waveform tried
      first, falls through to captions unchanged; `method`/`confidence` columns so old caption-only rows get
      re-evaluated instead of treated as settled. Found and fixed two pre-existing production bugs this
      surfaced: none of the 4 call sites ever passed a `client` — the whole "find a video counterpart" path,
      old and new, had never actually run; and `localcache.add_source` doesn't exist, the real function is
      `add_track_source` with a different signature. 8 new tests, full suite 2148 passed.)
- [x] Whisper last-resort branch (commit `cc2fef1`: `_try_whisper_offset()` transcribes the video's audio at
      temperature=0 and fuzzy-matches it against known synced audio lyrics via the same algorithm real
      captions use; opt-in only (`enable_whisper_fallback=False` default) since almost no tracks have real
      video captions and leaving it on by default would silently start full-track transcription for nearly
      the whole library. `--enable-whisper-fallback` CLI flag on the batch script. Found and fixed a deeper
      pre-existing bug: `get_lyrics_by_track_id(media_type="video")`'s fallback hands back the audio row
      mislabeled as video whenever a track has only one lyrics row (~10,929 of ~10,930) — the existing
      caption branch was silently self-aligning and storing a meaningless ~0.0 offset, a likely explanation
      for the "1236 rows are exactly 0.0" data note. Fixed with a content+source comparison guard. 12 new
      tests, full suite 2155 passed.)
- [x] PR — https://github.com/violetina/karaoke/pull/146 (5 commits, 15 files, +1218/-31, clean diff against
      `main` after fast-forwarding `origin/main` first so unrelated earlier work doesn't show up in the review)
