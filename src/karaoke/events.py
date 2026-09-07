"""Read the `karaoke-events` ledger written by Argo Events.

When a Celery post-processing task succeeds, the host worker emits a
`task.succeeded` event to RabbitMQ's ``celeryev`` exchange. Argo Events (in the
kind cluster) mirrors matching ``karaoke.tasks.*`` events and PUTs each one into
the OpenSearch ``karaoke-events`` index (see
``deploy/k8s/argo-events/celery-postprocess-sensor.yaml``).

The cluster cannot reach the host control API (it binds loopback), but both the
cluster and the host can reach OpenSearch — so OpenSearch is the shared ledger.
This module lets host-side UIs poll that ledger and refresh a finished track in
place, instead of doing a full page reload that would kill in-process work.
"""
from __future__ import annotations

from typing import Any, Optional

from .logger import log

EVENTS_INDEX = "karaoke-events"


def recent_events(since_ts: Optional[float] = None, limit: int = 50) -> list[dict[str, Any]]:
    """Return recent task events, newest first.

    Best-effort: an unreachable OpenSearch or a missing index yields ``[]`` so
    callers (TUI pollers) never crash on a degraded cluster.

    ``since_ts`` filters to events with ``ts`` strictly greater than the value,
    which is how a poller asks "anything new since I last looked?".
    """
    from .osclient import client

    query: dict[str, Any]
    if since_ts is not None:
        query = {"range": {"ts": {"gt": since_ts}}}
    else:
        query = {"match_all": {}}

    body = {
        "size": max(1, min(limit, 500)),
        "query": query,
        "sort": [{"ts": {"order": "desc"}}],
    }
    try:
        c = client()
        if not c.indices.exists(index=EVENTS_INDEX):
            return []
        res = c.search(index=EVENTS_INDEX, body=body)
    except Exception:
        log.debug("recent_events query failed", exc_info=True)
        return []

    out: list[dict[str, Any]] = []
    for hit in res.get("hits", {}).get("hits", []):
        src = hit.get("_source", {}) or {}
        out.append({
            "task_id": src.get("task_id"),
            "task_name": src.get("task_name"),
            "state": src.get("state"),
            "ts": _as_float(src.get("ts")),
        })
    return out


def _as_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
