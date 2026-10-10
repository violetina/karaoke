# Argo Events → OpenSearch ledger, and admin-TUI lifecycle patterns

Durable lessons from wiring Celery task events into Argo Events and making the
admin/web TUIs safe to close and refresh.

## Argo Events must DO something — a log-only Sensor is a no-op prototype
The initial integration had the Sensor spawn a `busybox` Job that only echoed
the payload. It proved the pipe but changed nothing; the user (rightly) called
it out as "not really doing anything". When asked to make events real, give the
Sensor a side effect that host UIs can observe.

## The cluster CANNOT reach the loopback host control API — use OpenSearch as a shared ledger
Key topology fact for this project: `karaoke-ctrl-api.service` binds
`127.0.0.1:8765`, so a pod inside the `kind-karaoke` cluster CANNOT reach it.
Verified empirically from a busybox pod: `172.18.0.1`, `host.docker.internal`,
and `10.0.2.2` on :8765 are all unreachable. Do NOT rebind the ctrl-api to the
docker bridge just to enable a cluster→host push — it spawns local processes and
must stay loopback-only.

The robust alternative: **OpenSearch is reachable from BOTH sides** — the cluster
via service DNS
(`opensearch-cluster-master.default.svc.cluster.local:9200`) and the host via
the NodePort at `localhost:9200`. So make OpenSearch the shared event ledger:
- Sensor uses an **HTTP trigger** (not a Job) that POSTs each `task.succeeded`
  event into a `karaoke-events` index. HTTP-trigger `payload:` entries map event
  fields (`dataKey: body.uuid` → `dest: task_id`, etc.). Building a per-doc URL
  via `parameters:` is fiddly — POST to `/karaoke-events/_doc/` and let
  OpenSearch assign the id, which is simpler and works.
- Host side reads it with `karaoke.events.recent_events(since_ts, limit)` (a
  best-effort OpenSearch `search`, returns `[]` on any failure/missing index),
  exposed as `GET /api/events/recent` on ctrl-api and `ApiClient.recent_events()`
  with a local fallback.
- `since_ts` builds a `{"range": {"ts": {"gt": since_ts}}}` query — that's how a
  poller asks "anything new since I last looked?".

### Verify the whole chain end-to-end (do this, don't guess)
```bash
UUID="smoke-$(date +%s)"
kubectl --context kind-karaoke -n karaoke exec deploy/rabbitmq -- \
  rabbitmqadmin publish exchange=celeryev routing_key=task.succeeded \
  payload="{\"name\":\"karaoke.tasks.event_smoke\",\"uuid\":\"$UUID\",\"state\":\"SUCCESS\",\"timestamp\":$(date +%s)}"
curl -s "http://localhost:9200/karaoke-events/_search?q=task_id:$UUID&pretty"   # cluster wrote it
curl -s "http://127.0.0.1:8765/api/events/recent?limit=5"                       # host reads it
```
Remember to `systemctl --user restart karaoke-ctrl-api.service` after adding the
endpoint (systemd runs cached code).

### Reload the web TUI WITHOUT killing tasks
A browser full-page reload tears down the `textual-serve` child process, which
fires `KaraokeTui.on_unmount` and kills in-process work: `_cancel_sample=True`
aborts a running sample (ffmpeg killed, partial wav deleted), `recorder.stop_all`
stops recordings, `_mic_stop.set()` ends the mic loop. Celery/systemd worker
tasks SURVIVE (they run on the host service, not in the TUI). The reload-free
path: a poller hits `/api/events/recent` and repaints the finished track's panel
IN PLACE via `call_from_thread`, instead of reloading the page.

## Making a Textual app easy to close
A bare `q` binding cannot quit while a text `Input` holds focus — Textual routes
the key into the field and types the letter. Provide reliable exits:
- Priority global quits regardless of focus. `from textual.binding import
  Binding`, then `Binding("ctrl+q", "quit", show=False, priority=True)` (+`ctrl+c`).
- An `escape` handler that unfocuses first, then quits:
  ```python
  def action_handle_escape(self) -> None:
      if isinstance(self.focused, Input):
          self.set_focus(None)   # first press: leave the field so `q` works again
          return
      self.exit()                # nothing focused: quit outright
  ```
- Add an explicit **Quit** button (web-TUI users may never learn keys).
- GOTCHA: `App.action_quit()` is a **coroutine**; calling it from a sync handler
  without awaiting is a silent no-op (Pyright: `reportUnusedCoroutine`). Use
  `self.exit()` from sync code.

## Panel containment: bound every panel, give the log its own scroll region
Symptom: a long error traceback spilled over/under neighbouring panels. Root
cause: the app had **no CSS** so widgets were unbounded. Fix (a `CSS = """..."""`
class attr):
- Each panel container gets `height: auto` (or a fixed row count) + a border.
- The error log gets its OWN fixed-height container with an inner scroll list:
  `#log-container { height: 1fr; min-height: 6; }` +
  `#error-list { height: 1fr; overflow-y: auto; }`.
- The whole stack scrolls on a short terminal:
  `#admin-workspace { layout: vertical; height: 1fr; overflow-y: auto; }`.
- Verify with `app.run_test(size=(w,h))`: the log widget `region` stays inside
  its container `region`, and `workspace.virtual_size.height` exceeds the
  viewport (scrolls, not clips).

## Connected-clients panel + graceful web-UI shutdown
- The browser web TUI is `scripts/web_serve.py` (textual-serve), NOT a systemd
  unit — one websocket per tab, no HTTP status endpoint. Count live tabs off the
  socket table: `ss -Htn state established '( sport = :8001 )'` (default PORT
  8001). Best-effort; never fatal if `ss` is missing.
- Active playback sessions: `ApiClient.list_play_sessions()`
  (`GET /api/play/sessions`).
- Graceful stop: `pgrep -f web_serve.py` then `os.kill(pid, SIGTERM)` so open
  tabs get a clean socket close. Never touch the admin process itself.

## Solo-repo doc convention (karaoke)
For this solo `violetina/karaoke` repo, publish operator-facing schemas as their
own mkdocs page (e.g. `docs/platform-services.md` wired into `mkdocs.yml` nav),
and verify with `PYTHONPATH=src .venv/bin/mkdocs build` before committing.
