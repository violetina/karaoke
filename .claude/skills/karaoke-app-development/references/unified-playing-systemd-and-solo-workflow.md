# Unified playing, post-processing ops, and solo workflow

Session learning for the local `karaoke` application after the unified player/post-processing work.

## User/workflow preference
- This repo is Tina's personal solo project (`github.com/violetina/karaoke`, `/home/tina/karaoke`). Do not apply meemoo/team review gates by default.
- Preferred flow: fix in-place on `main`, or use a PR only when convenient and merge it immediately once local tests/CI pass. Do not leave PRs open for review unless Tina explicitly asks.
- Always verify with `make test`; for ops/service changes, also verify the live unit/command actually runs.

## Duplicate-track prevention for transcribe/write-through
When adding/updating tracks with a source URL, match existing records by source URL or YouTube video ID before falling back to fuzzy artist/title. This prevents Whisper/transcribe paths from creating duplicates when metadata comes from filenames or remix titles (e.g. a tag-less `FFxsTiFGWT8` file) while the URL points to a canonical track.

## Post-processing and lyric-following diagnosis
If lyrics do not follow smoothly:
1. Check whether cached lyrics have word-level Enhanced LRC tags (`<00:...>word`) or only line-level LRC (`[00:...] whole line`).
2. Use `needs_postprocessing(track_id, conn)` to determine missing `analysis` / `timings`.
3. Ensure RabbitMQ port-forward + worker are up (`make systemd-status`, `make health`, or journalctl for `karaoke-postprocess`).
4. For videos with captions, the post-processing worker can upgrade word timings. For videos with no captions, line-level timing is the best non-ASR result.
5. Whisper on chopped/pitched/remixed music can hallucinate or align poorly; prefer preserving accurate LRCLIB line-level lyrics over replacing them with bad word timings.

## yt-dlp EJS remote components
Modern YouTube can expose only storyboards unless JS signature/`n` challenges are solved. The durable fix is to enable yt-dlp remote components (`remote_components=['ejs:github']`, CLI `--remote-components ejs:github`) and have a JS runtime such as `deno` on PATH. In this repo `_ejs_opts()` centralizes that and `KARAOKE_YTDLP_REMOTE_COMPONENTS` can override it.

## systemd user services
For the host-side karaoke stack, use systemd `--user` units so logs are in journald and everything can be health-checked:
- `karaoke.target` umbrella
- API (`:8000`), control API (`:8765`), RabbitMQ port-forward (`:5672/:15672`), postprocess worker
- `karaoke-healthcheck.service` + timer that checks APIs, AMQP, kind pods, Chrome CDP, and SQLite

Useful commands:
- `make systemd-install`, `make systemd-up`, `make systemd-status`, `make systemd-down`, `make health`
- `journalctl --user -u 'karaoke-*' --since '10 min ago'`
