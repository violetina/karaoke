# How-To: Event-driven workflow hooks with Argo Events

This project keeps heavy post-processing in Celery, then uses Argo Events as a
Kubernetes-native event layer that reacts to Celery task lifecycle events.

Argo Events subscribes to Celery's `celeryev` AMQP topic exchange. It does
**not** consume the Celery work queue (`karaoke-postprocess-celery`), so it can
never steal post-processing jobs from `karaoke-celery-worker.service` — it only
observes a separate, mirrored copy of task events.

## What the event hook actually does

When a `karaoke.tasks.*` task **succeeds**, the Sensor indexes a small event
document into the OpenSearch `karaoke-events` index. That index is the shared
event ledger between the cluster and the host:

- The **cluster** writes to it via the in-cluster service DNS
  (`opensearch-cluster-master.default.svc.cluster.local:9200`).
- The **host** reads it via the NodePort at `localhost:9200`, surfaced through
  the control API endpoint `GET /api/events/recent`.

This indirection exists because the host control API binds to loopback
(`127.0.0.1:8765`) and is unreachable from inside the kind cluster, whereas
OpenSearch is reachable from both sides. A host-side TUI / web-UI poller reads
`/api/events/recent` and refreshes the just-finished track in place (new
key/BPM/lyrics) **without a full page reload** — a reload would tear down the
`textual-serve` child process and kill any in-progress sample/record/mic work.

## Current flow

```text
TUI/API selects song
  -> enqueue_if_needed()
  -> Celery task queue: karaoke-postprocess-celery
  -> host Celery worker runs postprocess_track + subtasks
  -> Celery emits task events to RabbitMQ exchange: celeryev
  -> Argo Events AMQP EventSource mirrors matching task.succeeded events
  -> Sensor HTTP-PUTs the event into OpenSearch index: karaoke-events
  -> host GET /api/events/recent reads the ledger
  -> TUI / web UI refreshes the finished track in place (no reload)
```

## Prerequisites

Argo Events is installed in the `argo-events` namespace with a default EventBus:

```bash
kubectl --context kind-karaoke create namespace argo-events
kubectl --context kind-karaoke apply -f https://raw.githubusercontent.com/argoproj/argo-events/stable/manifests/install.yaml
kubectl --context kind-karaoke apply -n argo-events -f https://raw.githubusercontent.com/argoproj/argo-events/stable/examples/eventbus/native.yaml
```

RabbitMQ credentials are supplied as a secret in `argo-events`. Do not commit
real credentials to the repo.

```bash
kubectl --context kind-karaoke -n argo-events create secret generic rabbitmq-secret \
  --from-literal=username="$RABBITMQ_USER" \
  --from-literal=password="$RABBITMQ_PASS"
```

For the current local dev deployment, RabbitMQ still uses the default guest
credentials unless overridden.

## Apply the karaoke event hooks

```bash
kubectl --context kind-karaoke apply -k deploy/k8s/argo-events
```

This creates:

- `EventSource/amqp-celery-eventsource` — subscribes to RabbitMQ exchange
  `celeryev` with routing key `task.succeeded`, using a separate durable queue
  `karaoke-celery-events`.
- `Sensor/celery-postprocess-sensor` — filters to karaoke Celery task names and
  HTTP-PUTs each event into the OpenSearch `karaoke-events` index.
- `ServiceAccount`, `Role`, and `RoleBinding` (kept for future Job-based
  triggers).

The `karaoke-events` index is created on first write, or explicitly:

```bash
curl -X PUT http://localhost:9200/karaoke-events -H 'Content-Type: application/json' -d '{
  "mappings": {"properties": {
    "task_id":{"type":"keyword"}, "task_name":{"type":"keyword"},
    "state":{"type":"keyword"}, "ts":{"type":"double"},
    "raw":{"type":"object","enabled":false}
  }}
}'
```

## Verify

```bash
kubectl --context kind-karaoke -n argo-events get pods,eventsource,sensor
kubectl --context kind-karaoke -n karaoke exec deploy/rabbitmq -- \
  rabbitmqctl list_exchanges name type | grep celeryev
```

Safe synthetic smoke test, without enqueueing real post-processing work:

```bash
UUID="smoke-$(date +%s)"
kubectl --context kind-karaoke -n karaoke exec deploy/rabbitmq -- \
  rabbitmqadmin publish exchange=celeryev routing_key=task.succeeded \
  payload="{\"name\":\"karaoke.tasks.event_smoke\",\"uuid\":\"$UUID\",\"state\":\"SUCCESS\",\"timestamp\":$(date +%s)}"

# The event should land in OpenSearch within a couple of seconds:
curl -s "http://localhost:9200/karaoke-events/_search?q=task_id:$UUID&pretty"

# And be visible through the host control API:
curl -s "http://127.0.0.1:8765/api/events/recent?limit=5"
```

Expected: the OpenSearch hit and the `/api/events/recent` response both contain
the `task_id`, `task_name`, and `state` of the published event.

## Important details

- Celery workers must emit task events. The app sets
  `worker_send_task_events=True` and `task_send_sent_event=True`, and the worker
  command includes `--events`.
- The Sensor intentionally listens only for `task.succeeded` to avoid writing an
  event document for every sent/received/started lifecycle event.
- The event hook observes `celeryev`, not `karaoke-postprocess-celery`. Consuming
  the work queue directly would race with the Celery worker and break processing.
- OpenSearch is the ledger because the host control API binds loopback and is
  unreachable from the cluster, while OpenSearch is reachable from both sides.
- Restart the control API after changing the endpoint code — systemd runs cached
  code: `systemctl --user restart karaoke-ctrl-api.service`.
- Later sensors can additionally trigger notifications, workflow cleanup, or Argo
  Workflows; the RBAC for Job creation is left in place for that.

