"""Read-only status probe for the post-processing pipeline (RabbitMQ + worker).

Feeds the TUI a compact health/load read-out:

- queue depth (ready / unacked), consumer count, delivery rate — via the RabbitMQ
  management HTTP API (default http://localhost:15672).
- worker CPU% and RSS — by finding the ``karaoke.postprocess_worker`` process on
  the host and sampling ``/proc/<pid>/stat`` over a short interval.

Everything is best-effort: any failure yields ``available=False`` with a reason,
never an exception, so the TUI never breaks when RabbitMQ or the worker is down.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

QUEUE_NAME = "karaoke-postprocess"


@dataclass
class PostprocessStatus:
    available: bool = False
    reason: str = ""
    # queue
    ready: int = 0
    unacked: int = 0
    consumers: int = 0
    deliver_rate: float = 0.0
    publish_rate: float = 0.0
    # worker
    worker_running: bool = False
    worker_cpu: Optional[float] = None   # percent of one core
    worker_rss_mb: Optional[float] = None

    @property
    def busy(self) -> bool:
        """True when there's outstanding work or a task is in flight."""
        return self.unacked > 0 or self.ready > 0


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


def _find_worker_pid() -> Optional[int]:
    """Find the postprocess worker PID by scanning /proc cmdlines (Linux)."""
    proc = "/proc"
    if not os.path.isdir(proc):
        return None
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        try:
            with open(f"{proc}/{name}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ").decode(errors="ignore")
        except OSError:
            continue
        if "postprocess_worker" in cmd:
            return int(name)
    return None


def _proc_cpu_times(pid: int) -> Optional[tuple[int, int]]:
    """Return (process_jiffies, total_system_jiffies) for a CPU% sample."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            parts = fh.read().split()
        utime, stime = int(parts[13]), int(parts[14])
        with open("/proc/stat") as fh:
            cpu_line = fh.readline().split()[1:]
        total = sum(int(x) for x in cpu_line)
        return utime + stime, total
    except (OSError, ValueError, IndexError):
        return None


def _proc_rss_mb(pid: int) -> Optional[float]:
    try:
        with open(f"/proc/{pid}/statm") as fh:
            pages = int(fh.read().split()[1])  # resident set size in pages
        return pages * (os.sysconf("SC_PAGE_SIZE") / (1024 * 1024))
    except (OSError, ValueError, IndexError):
        return None


def _worker_cpu_percent(pid: int, interval: float = 0.15) -> Optional[float]:
    """Sample CPU% (of one core) over a short interval."""
    s1 = _proc_cpu_times(pid)
    if s1 is None:
        return None
    time.sleep(interval)
    s2 = _proc_cpu_times(pid)
    if s2 is None:
        return None
    dproc = s2[0] - s1[0]
    dtotal = s2[1] - s1[1]
    if dtotal <= 0:
        return 0.0
    ncpu = os.cpu_count() or 1
    return max(0.0, min(100.0 * ncpu, 100.0 * ncpu * dproc / dtotal))


def get_status(*, sample_cpu: bool = True) -> PostprocessStatus:
    """Best-effort snapshot of the post-processing pipeline."""
    st = PostprocessStatus()

    # Worker process (independent of RabbitMQ reachability).
    pid = _find_worker_pid()
    if pid is not None:
        st.worker_running = True
        if sample_cpu:
            st.worker_cpu = _worker_cpu_percent(pid)
        st.worker_rss_mb = _proc_rss_mb(pid)

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
    st.ready = int(data.get("messages_ready", 0) or 0)
    st.unacked = int(data.get("messages_unacknowledged", 0) or 0)
    st.consumers = int(data.get("consumers", 0) or 0)
    stats = data.get("message_stats", {}) or {}
    st.deliver_rate = float(stats.get("deliver_get_details", {}).get("rate", 0.0) or 0.0)
    st.publish_rate = float(stats.get("publish_details", {}).get("rate", 0.0) or 0.0)
    return st


def _cpu_bar(cpu: Optional[float], width: int = 10) -> str:
    """ASCII meter for CPU% of one core (0..100)."""
    if cpu is None:
        return "─" * width
    filled = max(0, min(width, round(cpu / 100.0 * width)))
    return "█" * filled + "░" * (width - filled)


def worker_load_line(st: PostprocessStatus, bar_width: int = 10) -> str:
    """One-line ASCII summary of the post-processing pipeline for the TUI.

    Examples:
        worker-load: [████░░░░░░]  38% cpu · queue 0 · idle
        worker-load: [█████████░] 92% cpu · queue 3 (1 busy) · working
        worker-load: worker down · broker unreachable
    """
    parts: list[str] = []
    if st.worker_running:
        bar = _cpu_bar(st.worker_cpu, bar_width)
        cpu = f"{st.worker_cpu:>3.0f}% cpu" if st.worker_cpu is not None else " -- cpu"
        parts.append(f"[{bar}] {cpu}")
    else:
        parts.append("worker down")

    if st.available:
        q = f"queue {st.ready}"
        if st.unacked:
            q += f" ({st.unacked} busy)"
        parts.append(q)
        parts.append("working" if st.busy else "idle")
    else:
        parts.append("broker unreachable")

    return "worker-load: " + " · ".join(parts)
