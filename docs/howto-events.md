# How-To: Event-driven workflow hooks with Argo Events

This project keeps heavy post-processing in Celery, then uses Argo Events as a
Kubernetes-native event layer that can react to Celery task lifecycle events.

The first event integration is intentionally observational and safe: Argo Events
subscribes to Celery's `celeryev` AMQP topic exchange and triggers a tiny logging
Job for karaoke task events. It does **not** consume the Celery work queue, so it
cannot steal post-processing jobs from `karaoke-celery-worker.service`.

## Current flow

```text
TUI/API selects song
  -> enqueue_if_needed()
  -> Celery task queue: karaoke-postprocess-celery
  -> host Celery worker runs postprocess_track + subtasks
  -> Celery emits task events to RabbitMQ exchange: celeryev
  -> Argo Events AMQP EventSource mirrors matching task.# events
  -> Sensor triggers a Kubernetes Job (currently logs the event payload)
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
  triggers a short-lived logging Job.
- `ServiceAccount`, `Role`, and `RoleBinding` allowing the Sensor to create Jobs
  in `argo-events`.

## Verify

```bash
kubectl --context kind-karaoke -n argo-events get pods,eventsource,sensor
kubectl --context kind-karaoke -n karaoke exec deploy/rabbitmq -- \
  rabbitmqctl list_exchanges name type | grep celeryev
```

Trigger a karaoke post-processing task, then inspect the Sensor and created Jobs:

```bash
kubectl --context kind-karaoke -n argo-events get jobs
kubectl --context kind-karaoke -n argo-events logs job/<job-name>
```

Safe synthetic smoke test, without enqueueing real post-processing work:

```bash
kubectl --context kind-karaoke -n karaoke exec deploy/rabbitmq -- \
  rabbitmqadmin publish exchange=celeryev routing_key=task.succeeded \
  payload='{"name":"karaoke.tasks.event_smoke","uuid":"argo-events-smoke","state":"SUCCESS"}'

kubectl --context kind-karaoke -n argo-events get jobs
kubectl --context kind-karaoke -n argo-events logs job/<job-name>
```

Expected Job log:

```text
received Celery task event
payload: {"name":"karaoke.tasks.event_smoke","uuid":"argo-events-smoke","state":"SUCCESS"}
```

## Important details

- Celery workers must emit task events. The app sets
  `worker_send_task_events=True` and `task_send_sent_event=True`, and the worker
  command includes `--events`.
- The prototype Sensor intentionally listens only for `task.succeeded` to avoid
  creating a Kubernetes Job for every sent/received/started event.
- The event hook observes `celeryev`, not `karaoke-postprocess-celery`. Consuming
  the work queue directly would race with the Celery worker and break processing.
- The first Sensor is deliberately a logging trigger. Later sensors can trigger
  OpenSearch refreshes, notifications, workflow cleanup, or Argo Workflows once
  the event shape is verified.
