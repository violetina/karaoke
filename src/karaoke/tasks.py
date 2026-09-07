"""Celery tasks for karaoke background processing.

Phase 1 keeps behaviour identical to the legacy pika worker by wrapping
`postprocess_worker.process_task()`. That gives us Celery's retries, dashboard
visibility and worker controls without rewriting the proven audio/lyrics logic
in the same change. Later phases can split this into chainable download/analyse/
sync/index tasks.
"""
from __future__ import annotations

from typing import Any, cast

from .celery_app import app
from .logger import log


@app.task(
    bind=True,
    name="karaoke.tasks.postprocess_track",
    autoretry_for=(RuntimeError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=2,
)
def postprocess_track(self, payload: dict[str, Any]) -> dict[str, Any]:
    """Run one post-processing job via the existing worker implementation.

    Payload shape is the same as the legacy queue body: `{artist, title, url}`.
    The task is deliberately small: all DB idempotency, download decisions,
    analysis, lyric sync, timing upgrades and failure classification stay in
    `postprocess_worker.process_task()` for phase 1.
    """
    from . import postprocess_worker

    artist = (payload.get("artist") or "").strip()
    title = (payload.get("title") or "").strip()
    log.info("celery postprocess: received %s - %s", artist, title)
    postprocess_worker.process_task(payload)
    return {"status": "ok", "artist": artist, "title": title}


def enqueue_postprocess(payload: dict[str, Any]) -> str:
    """Publish a Celery post-processing task and return its task id."""
    result = cast(Any, postprocess_track).delay(payload)
    return str(result.id)
