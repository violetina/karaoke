"""Celery application for karaoke background workflows.

Celery is the new orchestration layer for heavy, retryable background work. It
uses the existing RabbitMQ broker exposed on localhost by `karaoke-mq-forward` /
`make mq-port-forward`, but keeps Celery messages on their own queue so the
legacy pika worker cannot consume an incompatible protocol body.
"""
from __future__ import annotations

import os
from urllib.parse import quote

from celery import Celery

POSTPROCESS_QUEUE = os.environ.get(
    "KARAOKE_CELERY_POSTPROCESS_QUEUE", "karaoke-postprocess-celery")


def broker_url() -> str:
    """Return the Celery broker URL, defaulting to the existing RabbitMQ env."""
    if os.environ.get("CELERY_BROKER_URL"):
        return os.environ["CELERY_BROKER_URL"]
    host = os.environ.get("RABBITMQ_HOST", "localhost")
    port = os.environ.get("RABBITMQ_PORT", "5672")
    user = quote(os.environ.get("RABBITMQ_USER", "guest"), safe="")
    password = quote(os.environ.get("RABBITMQ_PASS", "guest"), safe="")
    vhost = quote(os.environ.get("RABBITMQ_VHOST", "/"), safe="")
    return f"amqp://{user}:{password}@{host}:{port}/{vhost}"


def result_backend_url() -> str | None:
    """Return the configured result backend, if any.

    Celery can publish and run fire-and-forget work with no result backend. That
    keeps phase 1 from introducing Redis. If richer chains/chords need stored
    results later, set CELERY_RESULT_BACKEND (for example to redis://...).
    """
    return os.environ.get("CELERY_RESULT_BACKEND") or None


app = Celery("karaoke", broker=broker_url(), backend=result_backend_url())
app.conf.update(
    task_default_queue=POSTPROCESS_QUEUE,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    task_routes={
        "karaoke.tasks.postprocess_track": {"queue": POSTPROCESS_QUEUE},
    },
    timezone="UTC",
)

# Import task module after app creation so decorators bind to this app.
app.autodiscover_tasks(["karaoke"])
