# Changelog

## 1.7.0 — OpenSearch Dashboards, Mood/Chords v2 search, and artist cleanup

### OpenSearch Dashboards (new)

- Deploy OpenSearch Dashboards 3.8.0 into `kind-karaoke` (`make dashboards-install`), reachable on `http://localhost:5601` through the `karaoke-dashboards-forward` user unit. Localhost only, no auth, like the OpenSearch it fronts. `make dashboards-import` regenerates and imports the saved objects; `make dashboards-uninstall` removes it.
- Four dashboards: **Artists Overview** (type, genre, country, decade, labels, track counts), **Lyrics and Library Coverage** (synced vs unsynced, lyrics source, per-artist gaps), **Lyric Lines Search** (full-text over every lyric line) and **Audio Analysis** (Mood v2 and Chords v2).
- Index patterns ship with their field lists, so visualizations resolve fields without a manual "refresh field list".

### Mood v2 and Chords v2 in search

- The `tracks` index now carries the sound-derived fields: `audio_mood`, `audio_feeling`, `mood_arc`, `feeling_arc`, `mood_swings_per_minute`, `mood_share_*`, `harmony_style`, `key_mode`, `blues_score`, `ii_v_i`, `tritone_subs` and related. Only current-version rows are indexed; `dominant_mood` remains the lyric-sentiment mood.
- `embed_clap` refreshes a track's audio fields as soon as its mood is stored (mood is the last step of the chain).

### Artist and library cleanup

- Stop the now-playing path re-creating uploader-named tracks such as "Vidiot411 - Roger And Out": it matches by source URL first, then to a unique resolved track when the artist is an unmatched uploader handle.
- `folder_scan` no longer takes a bare track number as the artist; it parses `NN - Artist - Title` filenames and falls back to the parent artist folder.
- `db_cleanup` gains a second pass (`find_channel_artist_dupes.py`) that classifies cross-artist duplicates (typos, reordered collaborations, uploader handles) using MusicBrainz lookup status, repairs numeric artists from their file paths and merges the safe ones. Covers by other artists and collaboration subsets are left alone. `merge_tracks` now carries genre, mood, chords, tone, art, artist links, sync offsets, notes, playlist and radio references, duration and play counts.

### Post-processing

- The CLAP worker runs 4 processes instead of 1 (about 2.4x mood throughput on a 24-core host); each holds its own model copy (about 1.5 GB resident).
- Add `scripts/enqueue_mood_chords.py`: a bounded batch of tracks missing current Mood/Chords v2, grouped by primary MusicBrainz artist with `--exclude-top`.
- `fill_library_gaps.py` takes a single-instance lock, passes browser cookies to downloads and flushes progress.

### Dashboard and prompter

- Show unsynced lyrics as text-only lines with a "Text-only (unsynced)" tag instead of an empty prompter.
- AI DJ prompt and parsing hardening since 1.6.0: no invented titles, placeholders or side-projects, malformed `[QUEUE]` brackets and quotes are stripped before track resolution, the booth adapts when nothing is playing, bad YouTube artist names resolve to their canonical identities, and the prompt is seeded with a random selection of library tracks and playlists. New smart playlists for borderline moods and discovery.
- Fix the genre gap-fill retrying blank verdicts forever, and make Backstage gap-fill vector only the missing tracks.
- AI DJ: model selection endpoints (`/api/dj/models`), prompt grounding with facts retrieved for the user's query, and relaxed "Artist - Title" matching when queueing.
- Refresh `webapp_dist`; the previous refresh removed the old bundles without adding the new ones.
- Drop the retired Gemini 1.5 Flash and Gemma 3 27B options from live captions.

### Running and upgrading

- Restart `karaoke-celery-worker` and `karaoke-celery-clap` after upgrading (`systemctl --user daemon-reload` first, for the new CLAP unit), and `karaoke-dashboard`. Run `make systemd-install` to link the new `karaoke-dashboards-forward` unit.
- Purging a Celery queue does not release in-flight song claims; re-queuing the same tracks within 15 minutes is skipped as "already in progress".

## 1.6.0 — Backstage admin page, A/V sync, and queue autopilot

### Backstage (new)

- Add a "Backstage" page to the Vue dashboard, porting the admin TUI's (`admin_tui.py`) operations screen to the web: Celery worker status with scale controls, connected Web TUI/playback clients, live recording start/stop, kiosk Chrome restart, radio session listing and one-click import, the lyrics staging queue with approve/reject, and recent events/error-log viewers.
- Add songrec radio mode control and a main playback transport card (previous/play-pause/next/seek ±10s, now-playing status) to Backstage, reusing the dashboard's existing player/radio endpoints.
- Add a library/playlist browse table (search the library or pick a saved playlist) with a right-click context menu per track: Process, Reprocess (forces every pipeline step), Try Whisper (forces lyric re-sync), and A-V Sync (new `karaoke.tasks.sync_av` Celery task, force-re-verifies the waveform Audio/Video offset).
- Add a Live Captions panel with a Whisper/Gemini model chooser and language selector, streaming transcription over SSE.
- Add the **Line Looper**: a manual lyric word-timing correction tool. Loop a line's current estimate, nudge the start in ±50/500ms steps until it lands on the first word, play through once and mark the last word heard, then calculate interior word times from the two confirmed anchors using the same weighting rules (word/character count plus punctuation pauses) `lyric_align.line_weight` already applies to whole lines — not an even split. Plays the track's own locally cached audio/video file directly in the page. Saving automatically advances to the next line.
- Wire `scripts/enqueue_playlists.py` (bulk-process every track in a saved playlist) and `scripts/fill_analysis_and_vector_gaps.py` (library-wide missing Key/BPM/genre/vector backfill, launched as a transient systemd unit with start/stop/status and a live log tail) into Backstage buttons. Fixed a latent bug in the gap-fill script found by actually running it: it built SQLite-style `?` placeholders against the Postgres backend and crashed immediately; now uses a parameterized `= ANY(%s)`.

### Audio/Video sync

- Add a waveform cross-correlation aligner to detect the time offset between a track's audio-oriented and video-oriented YouTube sources, integrated as the first-choice method ahead of caption-based alignment, with an opt-in Whisper transcript as a last resort.
- Skip the waveform attempt on clear duration mismatches (e.g. a much longer "intro" video) using a threshold tied to the aligner's own search window, so the two settings can't drift apart.
- Fix the Audio/Video toggle reading and exposing the wrong state; add a `force_waveform` re-verify option to bypass the "already has an offset" skip for re-checking stale, pre-waveform offsets.
- Fix `track_id`-targeted batch syncing to accept multiple `--track-id` flags.

### AI DJ, queue, and playback

- Add a "Keep going" autopilot toggle (Playlists and Queue panels) that auto-refills the local queue with fitting tracks as it nears the end, and forces YouTube Music's own Autoplay off while it's active.
- Add a dashboard queue follower service that drives and advances the local queue; make Next/Previous instant (one load instead of a YouTube Music detour).
- Show the Up Next queue (next 4 tracks) and a next-lyric-line countdown on the dashboard prompter; show an instrumental-break rest marker.
- Stop the AI DJ booth from suggesting the song already playing; seed DJ suggestions on the resolved track_id rather than artist/title text.
- Document the weights and empirical validation behind both next-song-suggestion algorithms (`dj_suggest.py`'s single-next-track strategies, `queue_suggest.py`'s whole-queue autopilot refill) in `docs/next-song-suggestion.md`.

### Lyrics and Whisper fixes

- Fix `get_lyrics_by_track_id`'s audio/video fallback lookups being clobbered by a newer row of the other media type whenever its `source` string didn't match a hardcoded list — affected every track with both audio and AV-sync-derived video lyrics.
- Fix the stage view always serving the newest lyrics row regardless of the actual Audio/Video playback mode; it now reads the live AV mode before choosing which row to serve.
- Add a structural filter (`whisper_clean.py`) that drops a leading run of Whisper-transcribed spoken metadata (artist/title/album announced before the real lyrics start), requiring 2+ distinct metadata fields to avoid false positives on songs that legitimately open with their own title.

### Running and upgrading

- Restart `karaoke-dashboard.service`, `karaoke-ctrl-api.service`, and `karaoke-celery-worker.service` after upgrading; rebuild the frontend with `npm ci --prefix src/karaoke/webapp && npm run build --prefix src/karaoke/webapp` and hard-refresh the browser.
- The Line Looper and Backstage media playback read a track's already-locally-cached audio/video file directly (served with Range support by `ctrl_api.py` for seeking); a track with no cached file yet needs Processing first.
- The library gap-fill job can take minutes to hours on a large library; it runs as the transient `karaoke-backstage-gapfill` systemd user unit and is safe to start and leave running.

## 1.5.0 — Unified dashboard and artist identities

### Unified stage dashboard

- Add a Vue 3 dashboard backed by FastAPI on port 8900, with a prebuilt frontend and a `karaoke-dashboard` entry point.
- Combine highlighted live lyrics, the animated mood canvas with in-square chord overlays, mood-over-time summaries, song metadata, play counts, and saved-playlist membership.
- Embed the Ollama AI DJ chat beside the mood display. Answers cannot automatically queue or play songs; queue suggestions require an explicit button click.
- Provide player transport/seek controls, artwork served over HTTP, artist library tracks, and saved/smart playlist playback.
- Add artist/member/side-project and song-similarity graphs. Left-click inspects without interrupting playback; right-click offers explicit play, queue, DJ-list, and processing actions.
- Navigate artist relations by Wikidata IDs, support MusicBrainz IDs, and reject partial-name substitutions. Protect the artist pane from stale asynchronous responses.
- Refresh artist context on each song change, resolving missing artist links through the existing MusicBrainz/Wikidata pipeline.
- Add dashboard regression tests and frontend build/chat-safety CI checks.

### Artist metadata and search

- Resolve library artist strings to canonical MusicBrainz identities and track credits.
- Cache Wikidata artist facts and relations, derive genres from MusicBrainz votes, and index artist documents in OpenSearch.
- Link newly ingested tracks to known artists and enrich new artists during post-processing.
- Combine similarity facts in the constellation and improve required-fact searches and key/tempo links.

### Playlists and maintenance

- Increase smart-playlist capacity to 99 tracks, with genre-family, mood, harmonic-key, and tempo flow ordering.
- Improve main-genre ranking/filtering, CLAP genre coverage, and library-gap backfill controls.
- Include the opt-in dirty-record maintenance script and updated schema/API/Makefile documentation. The cleanup script is not run automatically.

### Running and upgrading

- Start the host control API (:8765) and MCP/graph server (:8888), then run `make dashboard` or `karaoke-dashboard` and open http://127.0.0.1:8900.
- The built frontend is included in the Python wheel. Rebuild source changes with `npm ci --prefix src/karaoke/webapp` and `make dashboard-build`.
- Restart the dashboard after upgrading Python code and hard-refresh the browser after frontend changes.
- OpenSearch supplies derived artist/search data; Ollama powers chat; existing Celery workers execute full song processing. Queueing requires a running YouTube Music kiosk and a queueable source.
- The dashboard is an unauthenticated host control surface. Use a trusted local network or set `DASHBOARD_HOST=127.0.0.1`; do not expose it directly to the internet.
