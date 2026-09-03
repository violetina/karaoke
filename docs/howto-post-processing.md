# How-To: Post-Processing (word timing + key/BPM analysis)

This guide covers the karaoke **post-processing pipeline** — the background system
that fills in two derived assets so lyrics *follow the music word-by-word* and the
visuals know each song's key and tempo:

1. **Word-level timing** — upgrades line-level synced lyrics (`[00:12.30] whole line`)
   to **Enhanced LRC** with per-word tags (`[00:12.30]<00:12.30>whole <00:12.80>line`).
   This is what makes the highlight track each word instead of jumping line-to-line.
2. **Audio analysis** — musical **key + tempo (BPM) + energy/brightness**, stored in
   the `track_analysis` table and shown in the TUI (Camelot wheel, rhythm bar, cartwheel).

The work is dispatched over a **RabbitMQ queue** running in the local kind cluster and
executed by a **host-side worker**. The queue is intentionally non-durable — if it's
reset, just refill it from SQLite.

---

## TL;DR

```bash
# One-time / whenever the cluster is fresh
kubectl --context kind-karaoke apply -k deploy/k8s   # deploy the RabbitMQ broker

# Each working session
make mq-port-forward         # terminal A: expose broker to host (localhost:5672)
make postprocess-worker      # terminal B: run the consumer (leave it running)
make postprocess-enqueue-all # terminal C (optional): queue every track that needs work
```

The TUI also **auto-enqueues** whatever song you play, so in normal use you only need
the broker + worker running; the queue fills itself.

---

## Why lyrics sometimes don't "follow"

When lyrics come from **LRCLIB** (or plain text), they're usually **line-level only**:
each line has one timestamp for its start, and the player interpolates word positions.
That's why the highlight can drift or jump. Post-processing looks for **YouTube json3
captions** on the track's source video and, when present, rewrites the cached lyrics as
Enhanced LRC with real per-word timings.

Check any track from the repo root:

```bash
PYTHONPATH=src .venv/bin/python -c "
from karaoke import localcache
from karaoke.postprocess_queue import needs_postprocessing
conn = localcache.connect()
tid = localcache.find_track_id('Ian Asher & Phantogram', 'Black Out Days (Stay Away)', conn)
print('track_id:', tid, 'needs:', needs_postprocessing(tid, conn))
"
# -> needs: ['analysis', 'timings']   (empty list == already complete)
```

> **Note:** word-timing needs the video to actually *have* captions. If the source has
> none, the upgrade returns `no-captions` and the line-level timing is kept as the best
> available. To force word timing on a caption-less track, transcribe it with Whisper
> instead (see `karaoke --youtube <url> --download --force-transcribe`).

---

## Architecture

```
 TUI plays a song ─┐
                   ├─► enqueue_if_needed() ──► RabbitMQ (kind cluster)
 make …-enqueue-all┘        (only if gaps)         │  queue: karaoke-postprocess
                                                   ▼
                                   karaoke-postprocess-worker (HOST)
                                     • download audio (yt-dlp)
                                     • analyze key/BPM/energy  ─► track_analysis
                                     • upgrade word timings     ─► lyrics (Enhanced LRC)
                                                   │
                                                   ▼
                                            SQLite (~/.local/share/karaoke)
```

**Why the worker runs on the host (not in-cluster):** it needs the local SQLite DB, the
YouTube audio cache, and the heavy analysis stack (essentia/librosa/whisper/yt-dlp) —
none of which belong in the slim API container image. Only the stateless **broker** runs
in kind.

Key modules:
- `src/karaoke/postprocess_queue.py` — `needs_postprocessing()` (gap detection),
  `enqueue_if_needed()` / `publish_postprocess_task()` (publish).
- `src/karaoke/postprocess_worker.py` — the consumer (`karaoke-postprocess-worker`).
- `scripts/enqueue_postprocess.py` — bulk backfill from SQLite.
- `deploy/k8s/rabbitmq.yaml` — broker Deployment + NodePort Service.

---

## Step-by-step

### 1. Deploy the broker (idempotent)
```bash
kubectl --context kind-karaoke apply -k deploy/k8s
kubectl --context kind-karaoke -n karaoke get pods   # rabbitmq-… should be Running
```

### 2. Expose the broker to the host
The pre-existing kind cluster has no AMQP port mapping (adding one needs a cluster
recreate, which would destroy OpenSearch), so we port-forward instead:
```bash
make mq-port-forward     # localhost:5672 (AMQP) + 15672 (management UI)
```
Leave this running. Management UI: http://localhost:15672 (guest/guest).

### 3. Run the worker
```bash
make postprocess-worker
```
It prints `Post-processing worker listening on queue 'karaoke-postprocess' …` and then
processes tasks as they arrive. Keep it running while you use the app.

### 4. Fill the queue
Either **play songs in the TUI** (auto-enqueues anything missing assets), or backfill
everything at once:
```bash
make postprocess-enqueue-all
```

### 5. Verify results
```bash
# queue drained?
kubectl --context kind-karaoke -n karaoke exec deploy/rabbitmq -- \
  rabbitmqctl list_queues name messages

# analysis written?
PYTHONPATH=src .venv/bin/python -c "
from karaoke import localcache, track_analysis
conn = localcache.connect()
tid = localcache.find_track_id('Ian Asher & Phantogram', 'Black Out Days (Stay Away)', conn)
print(track_analysis.get_analysis(tid, conn))
"
```

### Monitoring from the TUI

The TUI settings panel shows a live `worker-load:` line, refreshed every 3s:

```
worker-load: [████░░░░░░]  38% cpu · queue 0 · idle
worker-load: [█████████░] 92% cpu · queue 3 (1 busy) · working
worker-load: worker down · broker unreachable
```

- The ASCII bar is the worker process CPU% (of one core), read from `/proc`.
- `queue N` is ready messages; `(M busy)` is in-flight (unacked) tasks.
- `idle` / `working` reflects whether anything is queued or in flight.

Programmatic probe (same data the TUI uses):
```bash
PYTHONPATH=src .venv/bin/python -c "
from karaoke import postprocess_status as ps
print(ps.worker_load_line(ps.get_status()))
"
```
It reads the RabbitMQ **management API** (default `http://localhost:15672`,
forwarded alongside AMQP by `make mq-port-forward` / `karaoke-mq-forward.service`).

---

## Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `RABBITMQ_HOST` | `localhost` | Broker host for publisher + worker. |
| `RABBITMQ_USER` / `RABBITMQ_PASS` | `guest` / `guest` | Broker credentials. |
| `KARAOKE_COOKIES_FROM_BROWSER` | *(unset)* | Browser to pull YouTube cookies from (e.g. `firefox`) for Premium/age-restricted access. |
| `KARAOKE_YTDLP_REMOTE_COMPONENTS` | `ejs:github` | yt-dlp EJS challenge-solver components (see below). Set empty to disable. |

---

## Pitfalls & fixes

### "Requested format is not available" / only storyboards download
Modern YouTube requires solving JS **signature / `n` challenges** to expose real
audio/video formats. yt-dlp does this via **EJS remote components**, which need a JS
runtime (`deno` or `node`) installed on the host **and** the solver scripts enabled.
This project enables them by default (`KARAOKE_YTDLP_REMOTE_COMPONENTS=ejs:github`), so
downloads and caption probing work out of the box. If you see this error again:
- Ensure `deno` (or `node`) is installed and on `PATH`.
- Keep yt-dlp current: `.venv/bin/pip install -U yt-dlp`.
- To debug manually: `yt-dlp --list-formats <url> --remote-components ejs:github`.

### `timings upgrade → no-captions`
The source video has no usable captions. Word-timing can't be sourced; line-level timing
is retained. Use Whisper (`--force-transcribe`) if you need word timing anyway.

### `enqueue` silently does nothing
If the broker is unreachable, `enqueue_if_needed()` is a deliberate no-op (so the TUI
never blocks/crashes). Start `make mq-port-forward` first. Confirm reachability:
`(echo > /dev/tcp/localhost/5672) && echo ok`.

### Queue lost after a broker restart
Expected — it's non-durable by design. Rebuild it: `make postprocess-enqueue-all`.
