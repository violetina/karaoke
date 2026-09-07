"""Read-only status probe for the post-processing pipeline (RabbitMQ + worker).

Feeds the TUI a compact health/load read-out:

- queue depth (ready / unacked), consumer count, delivery rate — via the RabbitMQ
  management HTTP API (default http://localhost:15672).
- worker CPU% and RSS — by finding Celery post-processing workers (and legacy
  ``karaoke.postprocess_worker`` processes during rollback) on the host and
  sampling ``/proc/<pid>/stat`` over a short interval.

Everything is best-effort: any failure yields ``available=False`` with a reason,
never an exception, so the TUI never breaks when RabbitMQ or the worker is down.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional, List

QUEUE_NAME = os.environ.get("KARAOKE_POSTPROCESS_STATUS_QUEUE",
                            "karaoke-postprocess-celery")

ORCHESTRATOR_ENV = "KARAOKE_ORCHESTRATOR"

def orchestrator() -> str:
    """Selected background orchestrator (`celery` by default, `legacy` fallback)."""
    return os.environ.get(ORCHESTRATOR_ENV, "celery").strip().lower() or "celery"


@dataclass
class QueueDetails:
    name: str
    messages: int
    messages_ready: int
    messages_unacknowledged: int
    consumers: int


@dataclass
class WorkerUnitDetails:
    unit: str
    active: str
    running: bool


@dataclass
class PostprocessStatus:
    orchestrator: str = "celery"
    available: bool = False
    reason: str = ""

    dashboard_url: Optional[str] = None
    workers_active: int = 0
    queue_depth: int = 0
    queue_details: QueueDetails = field(default_factory=lambda: QueueDetails(name=QUEUE_NAME, messages=0, messages_ready=0, messages_unacknowledged=0, consumers=0))
    worker_details: List[WorkerUnitDetails] = field(default_factory=list)

    # Legacy fields, kept for TUI compatibility until the TUI moves to the new schema
    # These are not directly exposed in the /api/workers/status endpoint
    ready: int = 0
    unacked: int = 0
    consumers: int = 0
    deliver_rate: float = 0.0
    publish_rate: float = 0.0
    worker_running: bool = False
    workers: int = 0  # how many worker processes are up
    worker_pids: tuple[int, ...] = ()
    worker_cpu: Optional[float] = None  # summed percent of one core
    worker_rss_mb: Optional[float] = None  # summed resident MB
    cpu_sample: Optional[tuple] = None  # per-pid samples for the next delta

    @property
    def queued(self) -> int:
        """Total queue length: waiting (ready) + in-flight (unacked)."""
        return self.queue_details.messages_ready + self.queue_details.messages_unacknowledged

    @property
    def busy(self) -> bool:
        """True when there's outstanding work or a task is in flight."""
        return self.queue_details.messages_unacknowledged > 0 or self.queue_details.messages_ready > 0


def _mgmt_url() -> str:
    host = os.environ.get("RABBITMQ_MGMT_HOST", os.environ.get("RABBITMQ_HOST", "localhost"))
    port = os.environ.get("RABBITMQ_MGMT_PORT", "15672")
    return f"http://{host}:{port}/api/queues/%2F/{QUEUE_NAME}"


def _fetch_queue(timeout: float = 1.5) -> Optional[dict]:
    user = os.environ.get("RABBITMQ_USER", "guest")
    password = os.environ.get("RABBITMQ_PASS", "guest")
    req = urllib.request.Request(_mgmt_url())
    import base64
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def get_status() -> PostprocessStatus:
    """Best-effort snapshot of the post-processing pipeline.

    This version checks systemd service status and RabbitMQ queue details.
    """
    st = PostprocessStatus(orchestrator=orchestrator())
    st.dashboard_url = os.environ.get("KARAOKE_FLOWER_URL", "http://127.0.0.1:5555")

    # Check Celery worker service status
    celery_worker_unit = "karaoke-celery-worker.service"
    cmd = ["systemctl", "--user", "is-active", celery_worker_unit]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=2, check=True)
        worker_active = res.stdout.strip() == "active"
    except Exception:
        worker_active = False

    st.worker_details.append(WorkerUnitDetails(unit=celery_worker_unit, active="active" if worker_active else "inactive", running=worker_active))
    st.workers_active = 1 if worker_active else 0

    # Check Flower service status (optional, for dashboard URL)
    flower_unit = "karaoke-celery-flower.service"
    cmd = ["systemctl", "--user", "is-active", flower_unit]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=2, check=True)
        flower_active = res.stdout.strip() == "active"
        if not flower_active:
            st.dashboard_url = None # If flower is not active, dashboard is not available
    except Exception:
        st.dashboard_url = None

    # Queue via management API.
    try:
        data = _fetch_queue()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        st.reason = f"mgmt API unreachable: {exc}"
        return st
    if not data:
        st.reason = "no queue data"
        return st

    st.available = True
    st.queue_details.name = QUEUE_NAME
    st.queue_details.messages = int(data.get("messages", 0) or 0)
    st.queue_details.messages_ready = int(data.get("messages_ready", 0) or 0)
    st.queue_details.messages_unacknowledged = int(data.get("messages_unacknowledged", 0) or 0)
    st.queue_details.consumers = int(data.get("consumers", 0) or 0)
    st.queue_depth = st.queue_details.messages_ready + st.queue_details.messages_unacknowledged
    return st


def start_worker() -> bool:
    """Starts the Celery worker systemd service."""
    cmd = ["systemctl", "--user", "start", "karaoke-celery-worker.service"]
    try:
        subprocess.run(cmd, check=True, timeout=5)
        return True
    except Exception:
        return False


def stop_worker() -> bool:
    """Stops the Celery worker systemd service."""
    cmd = ["systemctl", "--user", "stop", "karaoke-celery-worker.service"]
    try:
        subprocess.run(cmd, check=True, timeout=5)
        return True
    except Exception:
        return False


def worker_load_line(st: PostprocessStatus, bar_width: int = 10) -> str:
    """One-line ASCII summary of the post-processing pipeline for the TUI.

    Examples:
        worker-load: [████░░░░░░]  38% cpu · queue 0 · idle
        worker-load: [█████████░] 92% cpu · queue 4 (1 busy) · working
        worker-load: worker down · broker unreachable
    """
    parts: list[str] = []
    if st.workers_active > 0:
        # For now, CPU bar is removed as we don't sample CPU directly in new schema
        cpu_info = "-- cpu" # Placeholder
        parts.append(f"[──────────] {cpu_info}")
    else:
        parts.append("worker down")

    if st.available:
        # Total queue length (waiting + in-flight); call out in-flight separately.
        q = f"queue {st.queue_depth}"
        if st.queue_details.messages_unacknowledged:
            q += f" ({st.queue_details.messages_unacknowledged} busy)"
        parts.append(q)
        parts.append("working" if st.busy else "idle")
    else:
        parts.append("broker unreachable")

    return "worker-load: " + " · ".join(parts)


def worker_panel(st: PostprocessStatus, width: int = 30) -> str:
    """Multi-line worker/queue read-out for the side panel.

    Labels are ASCII and left-aligned in a fixed column so the values line up
    whatever the terminal does with symbol glyphs — the same rule that keeps the
    sentiment bars aligned.

    Example::

        workers   1 up
        cpu       [####______]  --
        mem       -- MB
        queue     4  (1 busy)
        rate      -- in / -- out
    """
    # One wider than the longest label ("consumers"), so a value never abuts it.
    label_w = 10
    bar_w = max(6, min(12, width - label_w - 8))

    def row(label: str, value: str) -> str:
        return f"{label:<{label_w}s}{value}"

    lines = []
    if st.workers_active > 0:
        lines.append(row("workers", f"{st.workers_active} up"))
        lines.append(row("cpu", f"[──────────] --")) # CPU info removed from new schema
        lines.append(row("mem", f"-- MB")) # Memory info removed from new schema
    else:
        lines.append(row("workers", "none running"))

    if st.available:
        q = str(st.queue_depth)
        if st.queue_details.messages_unacknowledged:
            q += f"  ({st.queue_details.messages_unacknowledged} busy)"
        lines.append(row("queue", q))
        lines.append(row("consumers", str(st.queue_details.consumers)))
        lines.append(row("rate", f"-- in / -- out")) # Rate info removed from new schema
        lines.append(row("state", "working" if st.busy else "idle"))
    else:
        lines.append(row("broker", "unreachable"))

    return "\n".join(lines)
