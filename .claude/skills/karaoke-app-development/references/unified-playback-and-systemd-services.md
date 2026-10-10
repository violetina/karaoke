# Unified Playback, systemd Services & Solo Workflow

## 1. Solo Project vs Work Repos Workflow
- **Solo Repo (`karaoke`):** Tina is working solo on this platform. Do NOT leave PRs open for "team review". Fix in-place on `main` or open a PR and immediately merge with `gh pr merge --squash --delete-branch`.
- **Work Repos (`meemoo`):** Multi-developer team — always leave PRs open for review with rollback and live verification notes.

## 2. Unified Playback & Spotify Web Integration
- **All in One Window:** All playback (YouTube Music, regular YouTube, Spotify) routes into the dedicated Chrome player instance (`google-chrome --app=... --remote-debugging-port=9222`).
- **YouTube Music audio first:** When artist/title are known, do not navigate a
  direct `music.youtube.com/watch?v=...` link from browse/list mode. Use
  `open_song_url(..., artist=, title=, prefer_audio=True)` / `ApiClient.play(...)`
  so playback opens `music.youtube.com/search?q=artist+title`; direct watch links
  can land on the video side and drift out of sync.
- **CDP Navigation:** `try_chrome_cdp_navigate(url)` controls navigation over Chrome DevTools Protocol without spawning extraneous tabs.
- **Native Spotify Disabled:** Native Spotify desktop launching (`launch_spotify`) is deprecated in favor of web playback. Any `spotify:track:<id>` URI automatically converts to `https://open.spotify.com/track/<id>` and navigates the browser window.
- **Interchangeable Video IDs:** `find_track_by_url` extracts the 11-char YouTube video ID so `youtube` and `youtube_music` source records resolve interchangeably.

## 3. Host systemd --user Units & Health Checks
Instead of manual terminal commands, manage the host stack via systemd user services (`deploy/systemd/`):
- `karaoke-api.service`: FastAPI library read API (`:8000`)
- `karaoke-ctrl-api.service`: Playback control API (`:8765`)
- `karaoke-mq-forward.service`: RabbitMQ port-forward (`:5672` / `:15672`)
- `karaoke-postprocess@1..6.service`: RabbitMQ competing-consumer workers,
  all sharing `karaoke-postprocess.slice` (`CPUQuota=600%`, low CPU/IO weight)
- `karaoke.target`: Umbrella target to start/stop all services together (`make systemd-up`, `make systemd-down`)
- `karaoke-healthcheck.service` + `.timer`: Probes all 7 components every 5 minutes with retry logic to avoid false positives during boot races (`make health`).

## 4. Source-First Track Insertion & Whisper Transcriptions
- **URL-First Lookup:** `add_track_source()` always matches existing tracks by source URL / video ID before fuzzy artist/title. This prevents Whisper runs or remix variations from spawning duplicate database records.
- **Remix Timings Limitation:** Whisper forced alignment (`--lyrics-file`) and automated word timing cannot cleanly map original lyrics onto heavily chopped/pitch-shifted audio remixes (e.g. Ian Asher remix of Phantogram). In these cases, line-level LRCLIB timing is retained.
