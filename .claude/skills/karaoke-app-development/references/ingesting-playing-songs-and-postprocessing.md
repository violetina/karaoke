# Ingesting a Currently-Playing Song + Async Post-Processing

## Ingest the currently-playing YT Music / YouTube song via Whisper

Proven end-to-end recipe for "scan the current song and cache synced lyrics",
used repeatedly this session (Amy Winehouse *Tears Dry (Alt Demo)*, Dusty
Springfield *Spooky*, Bill Withers *Ain't No Sunshine*):

1. **Read what's playing:** `playerctl metadata` (or `-a` for all players). The
   YT Music tab reports an empty `xesam:artist` and a title like
   `"Ain't No Sunshine | YouTube Music"`.
2. **Find the video:** `youtube.search("<title>", limit=3)`; pick the official
   result. Confirm metadata with `youtube.fetch_metadata(url, download=False)`
   (its `title`/`uploader` give the real artist).
3. **Download + Whisper-transcribe in one shot** — do NOT try `karaoke -f`:
   ```bash
   .venv/bin/karaoke --youtube "<watch_url>" --download --force-transcribe \
       --cookies-from-browser firefox --print
   ```
   `resolve_youtube` supplies the correct artist/title AND sets the downloaded
   `path`, so `get_synced` transcribes the right file and caches under clean
   metadata. `--print` also dumps the LRC so you can eyeball it.
4. **Register BOTH source URLs** so scan mode resolves from either host (they
   share the 11-char video id, and `find_track_by_url` matches on it):
   ```sql
   INSERT OR IGNORE INTO sources (track_id, kind, url, player_name)
   VALUES (?, 'youtube_music', 'https://music.youtube.com/watch?v=<VID>', 'browser');
   ```
5. **Analyze key/BPM/energy** from the cached audio:
   `PYTHONPATH=src .venv/bin/python scripts/analyze_all_cached.py` — it maps
   `youtube` sources → cache webm by video id and skips already-analyzed tracks.

### Pitfall: `karaoke -f` has no `--artist`/`--title`
The main `karaoke` CLI rejects `--artist`/`--title` (`unrecognized arguments`).
A raw YouTube `.webm` also carries **no tags**, so `identify.from_file()` returns
`artist=""` and `title="<video_id>"`. Route metadata through `--youtube <URL>`
instead of `-f <file>` when you need a correct artist/title on the cached track.
(The `--artist`/`--title` overrides only exist on `karaoke-player`, not `karaoke`.)

### Pitfall: console script vs `python -m karaoke.cli`
`python -m karaoke.cli stage youtube <URL>` mis-parses `stage`/`youtube` as a
positional *query* and fails with `No synced lyrics for ... - stage youtube ...`.
Use the installed console entry point: `.venv/bin/karaoke-stage youtube <URL>`
(and `karaoke-stage approve <ID>`). The `*_main` subcommand parsers are wired to
the console scripts, not to `-m karaoke.cli`.

Whisper ASR on lo-fi/demo audio produces mondegreens (e.g. "my tea is dry" for
"my tears dry"). Offer to re-run with `--lyrics-file <plain.txt>` to keep Whisper
*timing* but correct *words*, or hand-fix the few mis-heard lines.

## Async post-processing queue (RabbitMQ in kind + host-side worker)

For deferred, slow derived assets (audio key/BPM/energy analysis; Enhanced-LRC
word-timing upgrades), the project uses a RabbitMQ work queue.

- **Broker in kind, worker on host.** `deploy/k8s/rabbitmq.yaml`
  (`rabbitmq:3-management`, NodePort 30567/30156) is stateless infra. The worker
  (`karaoke-postprocess-worker`) runs on the **host** because it needs the local
  SQLite DB, the YouTube audio cache, and the heavy analysis stack
  (essentia/whisper/yt-dlp) — none of which belong in the slim API image.
- **Queue is non-durable in practice.** If the broker resets, re-fill from SQLite
  with `make postprocess-enqueue-all` (scans every track, enqueues only those
  where `postprocess_queue.needs_postprocessing(track_id)` is non-empty).
- **Gating, not blind enqueue.** `needs_postprocessing()` returns `["analysis"]`
  (no `track_analysis` row / no bpm) and/or `["timings"]` (approved synced lyrics
  lack Enhanced `<mm:ss.xx>` word tags). `enqueue_if_needed()` publishes only when
  something is pending. The TUI calls it best-effort in a thread worker — a
  silent no-op if the broker is unreachable, so the UI never blocks/crashes.
- **Existing kind cluster has no AMQP port mapping.** Recreating the cluster to
  add one would destroy the OpenSearch release, so reach the broker with
  `make mq-port-forward` (forwards localhost:5672 + management 15672). The
  NodePort in the manifest only helps a freshly-created cluster.
- **Worker robustness:** prefer a real DB watch-source over a non-watchable
  payload URL (a YouTube *search* page has no extractable video id); ACK only
  after processing so a crash re-delivers; catch per-task exceptions so one bad
  video (e.g. yt-dlp "Requested format is not available") never kills the loop.

Run order: `kubectl apply -k deploy/k8s` → `make mq-port-forward` →
`make postprocess-worker` → (optional) `make postprocess-enqueue-all`.

## Worktree hygiene: don't sweep pre-existing uncommitted work into your PR
Before starting a feature, run `git status` in `/home/tina/karaoke`. The main
tree may already hold the user's **uncommitted in-progress work** (this session:
a "dedicated Chrome player" using CDP `Page.navigate` in `player_open.py`, plus
`mpris_name` plumbing and TUI bindings). Do NOT commit those alongside your
feature. Create a clean worktree from `origin/main`
(`git worktree add ../karaoke-<feat> -b feat/<feat> origin/main`), then COPY only
your new files into it and re-apply your own config edits there. Leave the user's
unrelated changes untouched in the main tree and offer to help commit them
separately.
