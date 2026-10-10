# API split, folder ingestion & exact backend function names

## Two-API split (keep new features on the right side)
- **`karaoke.api`** (library API, port 8000, cluster-deployable): READ-ONLY over
  SQLite + logs. No `xdg-open`/`playerctl`/`ffmpeg`/`songrec`. Endpoints: tracks,
  lyrics, stats, recordings *inspection*, workers, logs.
- **`karaoke.ctrl_api`** (control API, port 8765, loopback-only): anything needing
  the desktop session or local binaries — playback (`/api/play`), sample, record
  start/stop, and (added) player controls, folder scan, audio cut.
- Rule of thumb: if it shells out to `playerctl`/`ffmpeg`/`songrec`/spawns a
  process, it belongs in ctrl_api, NOT api. A future web UI drives ctrl_api over
  HTTP exactly as the TUI drives the same underlying functions — so put logic in a
  plain module (e.g. `folder_scan.py`) and have BOTH the CLI and the API endpoint
  call it. Don't bury logic inside an endpoint.

## Player controls already exist in `playerctl.py` (wrap, don't reinvent)
`list_players()`, `playing_players()`, `playing_player()` (status-aware; omit an
explicit player and target whoever is actually *Playing* so a paused kiosk tab
doesn't shadow the real source), `current_metadata(player)` -> `PlayerMetadata`,
`position()`, `status()`, `art_url()`, `pause()`, `play_pause()`, `next_track()`,
`previous_track()`, `seek(offset_s, player)`. Control fns return `bool`; surface a
falsey result as HTTP 409 ("no player could handle 'X'"), not a silent success.

## Local-file fingerprinting (Shazam on a file, not just live mic)
`identify.identify_file_fingerprint(path)`: ffmpeg-slice ~10s (`-ss 10 -t 10 -ac 1
-ar 16000`) then `songrec recognize -d <slice> -j`. Returns canonical
`(artist,title,album)` even when ID3 tags are missing/wrong. Guard on both
`shutil.which("songrec")` and `("ffmpeg")`; return None (never raise) when absent.

## folder_scan pipeline order (`folder_scan.scan_and_ingest_folder`)
tags (`tags.extract_tags`) -> fingerprint fallback only when artist/title weak
-> `analyze.analyze_audio` (key/BPM/energy) -> `clap_vector.embed_audio` + genre
-> resolve YT + Spotify -> SQLite ingest. Support `dry_run` (sync preview with
per-file `items`) vs real (dispatch to FastAPI `BackgroundTasks`, poll library API).

## EXACT function names / signatures (these bit me — verify before calling)
- Add track+source: `localcache.add_track_source(artist, title, *, album=, duration=, url=, kind=, conn=)` -> **returns track_id (int)**. There is NO `upsert_track` / `add_source`.
- `localcache.add_track_and_lyrics(artist, title, lyrics, album=, duration=, url=, kind=, conn=)` -> **returns None** (internally calls add_track_source). To get the id, call add_track_source first (or `find_track_id` after).
- `localcache.record_genre(track_id, verdict, conn)` — `conn` is POSITIONAL, not keyword.
- `source_select.select_best_source(candidates, artist, title, reference_duration=)` — candidates FIRST, duration is `reference_duration` (not `duration_s`).
- YouTube search is `youtube.search(query, limit=)` -> list[dict]; there is no `find_best_watch_url`.
- Spotify: `from .spotify_client import SpotifyClient; SpotifyClient().search_track(artist, title)` -> URI **string** (or None); needs OAuth creds, so wrap in try/except. Module is `spotify_client`, there is NO `spotify_lookup`.
- `analyze.analyze_audio(path)` -> `AudioAnalysis(key, key_confidence, key_agreement, bpm, method, energy, brightness, version)`.
- `track_analysis.save_detected(track_id, *, detected_key=, key_confidence=, key_agreement=, bpm=, method=, energy=, brightness=, analyzer_version=, conn=)`.

## Recording-analyse +vector pitfall (confirmed again)
`make recording-analyse ID=` runs under `.venv-audio`, which may lack the OpenSearch
stack -> stores key/BPM but no `+vector`. Re-run with the main venv when it has the
DSP stack: `PYTHONPATH=src .venv/bin/python -m karaoke.recording_worker --analyse ID --keep`.

## Testing pitfall: patch where the name is USED
`folder_scan.py` does `from .identify import identify_file_fingerprint`, so tests
must `patch("karaoke.folder_scan.identify_file_fingerprint")`, NOT
`patch("karaoke.identify.identify_file_fingerprint")`. Same for any
`from x import y` re-export.

## Flaky tests to ignore when unrelated
`tests/test_tui_layout.py` (Textual CSS/render snapshots) fails
non-deterministically under the full suite — different tests each run, all pass in
isolation. Not a regression signal for non-TUI changes; verify by running that file
alone rather than trusting a single full-suite failure.

## `.hermes/` is agent scratch — gitignore it
Plans written to `.hermes/plans/` should not be committed; `.hermes/` is in
`.gitignore`. `git add -A` will otherwise stage it.
