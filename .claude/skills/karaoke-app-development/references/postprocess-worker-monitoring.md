# Post-processing MQ: monitoring & worker-load read-out

Covers verifying the RabbitMQ post-processing pipeline and the TUI `worker-load:`
line added this session. Complements `references/rabbitmq-postprocessing-queue-and-spotify-chrome-unification.md`.

## Verifying the pipeline is working
```bash
# services up?
systemctl --user --plain list-units 'karaoke*' | grep -E "postprocess|mq-forward"
# AMQP reachable (port-forward alive)?
(echo > /dev/tcp/localhost/5672) && echo "5672 OK"
# queue depth + consumers (management API, port 15672 forwarded alongside 5672)
curl -s -u guest:guest "http://localhost:15672/api/queues/%2F/karaoke-postprocess" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('ready',d['messages_ready'],'unacked',d['messages_unacknowledged'],'consumers',d['consumers'])"
# end-to-end: publish a task, then watch it go idle->working(1 busy)->idle
```
Healthy = queue drains to 0 and `consumers == 1`.

## PITFALL: stray duplicate worker → 2 consumers
If the queue shows **2 consumers**, a manually-started worker
(`.venv/bin/python -m karaoke.postprocess_worker` in a terminal) is running
alongside the `karaoke-postprocess.service` one. Two workers race the same queue
and can double-process. Find and kill the stray:
```bash
ps aux | grep postprocess_worker | grep -v grep   # the pts/… one is the stray
kill <stray_pid>
```
Prefer the systemd unit as the single worker; don't leave `make postprocess-worker`
running in a terminal when the service is active.

## worker-load read-out in the TUI (LANDED)
`src/karaoke/postprocess_status.py` provides a best-effort probe (never raises):
- `get_status()` → `PostprocessStatus` with queue `ready`/`unacked`/`consumers`/
  `deliver_rate` (from the RabbitMQ **management API**, default
  `http://localhost:15672`, env `RABBITMQ_MGMT_HOST`/`RABBITMQ_MGMT_PORT`) plus
  worker `worker_running`/`worker_cpu`/`worker_rss_mb` (found by scanning `/proc`
  for `postprocess_worker` in the cmdline; CPU% sampled from `/proc/<pid>/stat`
  over a ~0.15s interval).
- `worker_load_line(st)` → pure formatter with an ASCII CPU bar, e.g.
  `worker-load: [████░░░░░░]  38% cpu · queue 0 · idle` /
  `... queue 3 (1 busy) · working` / `worker down · broker unreachable`.
- TUI: a `#worker-load` Static in the settings panel, refreshed every 3s via
  `set_interval(3.0, self._refresh_worker_load)`. The refresh runs the probe in a
  **background thread** (`run_worker(..., thread=True)`) because the CPU sample
  sleeps; the result is marshalled back with `self.call_from_thread(...)`.
- Tests: `tests/test_postprocess_status.py` (pure `_cpu_bar` / `worker_load_line` /
  `busy` — no live broker needed).

## Interpreting the numbers
Post-processing tasks are mostly **I/O-bound** (audio download, caption fetch), so
CPU% often reads ~0 even while `working`. Use `queue`/`(N busy)`/`working` as the
"is it doing something" signal; CPU% spikes mainly during the Essentia key/BPM
analysis step.
