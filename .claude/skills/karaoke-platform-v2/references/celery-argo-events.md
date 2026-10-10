# Celery + Argo Events integration notes

Use this when adding Kubernetes event hooks around the karaoke Celery post-processing pipeline.

## Safe event source design

Do not point Argo Events at the Celery work queue (`karaoke-postprocess-celery`). That queue is consumed by the host-side Celery worker; an Argo consumer there can steal jobs and break post-processing.

Instead, observe Celery's task-event exchange:

- RabbitMQ exchange: `celeryev`
- Exchange type: `topic`
- First safe routing key: `task.succeeded`
- Dedicated Argo queue: `karaoke-celery-events`

Celery must emit events:

- app config: `worker_send_task_events=True`
- app config: `task_send_sent_event=True`
- app config: `task_track_started=True`
- worker command includes `--events`

## Kubernetes context rule

Always include `--context kind-karaoke` on `kubectl` commands in this project. Tina works with several clusters, so an omitted context is a real safety bug.

## Verification pattern

Apply/dry-run the Argo bundle:

```bash
kubectl --context kind-karaoke apply --dry-run=server -k deploy/k8s/argo-events
kubectl --context kind-karaoke apply -k deploy/k8s/argo-events
kubectl --context kind-karaoke -n argo-events get pods,eventsource,sensor
```

Smoke test without enqueueing real post-processing work:

```bash
kubectl --context kind-karaoke -n karaoke exec deploy/rabbitmq -- \
  rabbitmqadmin publish exchange=celeryev routing_key=task.succeeded \
  payload='{"name":"karaoke.tasks.event_smoke","uuid":"argo-events-smoke","state":"SUCCESS"}'

kubectl --context kind-karaoke -n argo-events get jobs
kubectl --context kind-karaoke -n argo-events logs job/<job-name>
```

Expected log:

```text
received Celery task event
payload: {"name":"karaoke.tasks.event_smoke","uuid":"argo-events-smoke","state":"SUCCESS"}
```

## Pitfall: stale RabbitMQ bindings

If an EventSource was first created with a broad binding such as `task.#`, tightening the manifest to `task.succeeded` may leave the old durable queue binding behind. Verify bindings:

```bash
kubectl --context kind-karaoke -n karaoke exec deploy/rabbitmq -- \
  rabbitmqctl list_bindings source_name destination_name routing_key | \
  grep -E 'celeryev|karaoke-celery-events'
```

If the stale `task.#` binding remains, recreate the dedicated event queue:

```bash
kubectl --context kind-karaoke -n argo-events delete sensor celery-postprocess-sensor --ignore-not-found
kubectl --context kind-karaoke -n argo-events delete eventsource amqp-celery-eventsource --ignore-not-found
kubectl --context kind-karaoke -n karaoke exec deploy/rabbitmq -- rabbitmqctl delete_queue karaoke-celery-events
kubectl --context kind-karaoke apply -k deploy/k8s/argo-events
```

## Noise control

A prototype Sensor that reacts to all `task.#` events creates many Jobs because Celery emits sent/received/started/succeeded events. Start with `task.succeeded`; broaden later only when the trigger is no longer one Kubernetes Job per event.
