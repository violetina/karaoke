# API-first architecture & the ApiClient (portability for a future web UI)

The project is being shaped so the TUI, CLI, and a future web UI all drive the
**same backend over one client**. When adding a new capability, put the logic in
a plain module + an HTTP endpoint, then expose it through `ApiClient` — don't bury
it in `tui.py`.

## The two API services (keep the split)
- **Library API** (`karaoke.api`, default `:8000`) — read-only over SQLite,
  cluster-deployable. Tracks, lyrics, stats, recordings *inspection*, workers.
  Contains NO desktop-bound behaviour.
- **Control API** (`karaoke.ctrl_api`, default `:8765`, loopback only) — anything
  needing the desktop session: MPRIS via `playerctl`, `ffmpeg`, `songrec`,
  playback launch. Player controls, folder scan, audio cut, sample, record
  start/stop/analyse all live here.

Rule of thumb: if it needs playerctl / ffmpeg / songrec / a display, it belongs in
`ctrl_api`, not the library API (the library image is slim and containerized).

## ApiClient (`karaoke.api_client.ApiClient`)
One unified Python client wrapping both services. Key properties:
- Uses **stdlib `urllib`** only (no new deps).
- Honours `KARAOKE_API_HOST/PORT` and `KARAOKE_CTRL_HOST/PORT` (maps `0.0.0.0` → `localhost`/`127.0.0.1`).
- **In-process fallback**: each method tries HTTP first; if unreachable and
  `fallback_local=True`, it calls the underlying function directly. So standalone
  `karaoke-tui` keeps working with no server, yet the *same code path* goes over
  HTTP the moment `karaoke-api`/`karaoke-ctrl-api` run. This is what makes the web
  UI a drop-in later.
- Method naming maps 1:1 to endpoints: `list_players`, `get_current_player`,
  `player_play_pause/pause/next/previous/seek`, `play`, play-session helpers
  (`list_play_sessions/get_play_session/stop_play_session`), `sample`,
  `sample_stream_url`, `get_track_analysis_history`,
  `record_start/stop/status/analyse/discard_audio`, `scan_folder`, `audio_cut`,
  library reads, and `list_recordings/get_recording/update_recording`. Sample,
  scan, recording, postprocess, and CLI analysis upsert current `track_analysis`
  and append immutable `track_analysis_history` rows for version comparison.

## Wiring the TUI onto it
- `KaraokeTui.__init__` sets `self.api = ApiClient()`; actions call `self.api.*`.
- Migrate the clean request/response actions: player controls, record start/stop,
  browse/list playback, stats, recording analyse/discard. `POST /api/play`
  returns a `play_...` session handle and records recent sessions in the control
  API. `GET /api/sample/stream` is SSE (`start`, `progress`, `complete`,
  `error`) and `ApiClient.sample_stream_url(...)` returns the EventSource URL.
- **YouTube Music audio default**: browse/list playback should pass artist/title
  with `prefer_audio=True`. `open_song_url(..., artist=, title=)` opens
  `music.youtube.com/search?q=artist+title` instead of a direct `watch?v=` link;
  direct Music watch links can land on the video side, which is often out of sync
  with the album/audio track.

## Pitfalls hit this session
- **Module name drift**: it's `karaoke.spotify_client.SpotifyClient().search_track()`
  (returns a URI string), not a `spotify_lookup` module. Adding a source is
  `localcache.add_track_source(artist, title, url=, kind=, conn=)` (returns
  track_id); `add_track_and_lyrics(...)` returns None. `source_select.select_best_source(candidates, artist, title, reference_duration=)`
  takes candidates first.
- **Patch where a symbol is USED, not where defined**: `folder_scan` does
  `from .identify import identify_file_fingerprint`, so tests must patch
  `karaoke.folder_scan.identify_file_fingerprint`, not `karaoke.identify.…`.
- **TUI test helpers that use `KaraokeTui.__new__`** bypass `__init__`, so after
  adding `self.api`, set `app.api = ApiClient()` in the helper (e.g.
  `tests/test_recorder.py::_record_app`) or actions raise AttributeError. The
  fallback path then exercises the monkeypatched recorder functions.

## Local-file Shazam fingerprinting
`identify.identify_file_fingerprint(path)` fingerprints a local file: ffmpeg cuts
a ~10s slice (`-ss 10 -t 10 -ac 1 -ar 16000`) → `songrec recognize -d <wav> -j`.
Returns a `SongRef(source="fingerprint")`. Use it to recover canonical
artist/title when ID3 tags are missing/wrong (e.g. `track01.mp3`). Album is dug
out of `track.sections[0].metadata` where `title == "Album"`.

## Flaky Textual layout tests (known, pre-existing)
`tests/test_tui_layout.py` snapshot/CSS-render tests are timing-flaky: a
*different* ~3 fail on each full-suite run and **all pass in isolation**. Do not
chase them as regressions when your change didn't touch TUI layout/CSS. Confirm
by re-running the named tests alone before assuming your change broke them.
