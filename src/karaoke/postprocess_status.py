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
    # workers
    worker_running: bool = False
    workers: int = 0                     # how many worker processes are up
    worker_pids: tuple[int, ...] = ()
    worker_cpu: Optional[float] = None   # summed percent of one core
    worker_rss_mb: Optional[float] = None  # summed resident MB
    cpu_sample: Optional[tuple] = None   # per-pid samples for the next delta

    @property
    def queued(self) -> int:
        """Total queue length: waiting (ready) + in-flight (unacked)."""
        return self.ready + self.unacked

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


def find_worker_pids() -> list[int]:
    """Every postprocess worker PID, by scanning /proc cmdlines (Linux).

    Workers scale horizontally (``karaoke-postprocess@{1..N}``), so this
    deliberately returns all of them: reporting one worker's CPU while twelve
    are running would understate the load by an order of magnitude.
    """
    proc = "/proc"
    if not os.path.isdir(proc):
        return []
    pids = []
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        try:
            with open(f"{proc}/{name}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ").decode(errors="ignore")
        except OSError:
            continue
        # Match the module invocation, not any shell that merely mentions it.
        if "postprocess_worker" in cmd and "python" in cmd:
            pids.append(int(name))
    return sorted(pids)


def _find_worker_pid() -> Optional[int]:
    """First worker PID, or None. Kept for single-worker callers."""
    pids = find_worker_pids()
    return pids[0] if pids else None


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
    """Sample CPU% (of one core) over a short blocking interval.

    Prefer :func:`cpu_percent_delta` for repeated polling (no sleep); this is a
    convenience for one-off callers.
    """
    s1 = _proc_cpu_times(pid)
    if s1 is None:
        return None
    time.sleep(interval)
    s2 = _proc_cpu_times(pid)
    if s2 is None:
        return None
    return _cpu_from_samples((pid,) + s1, (pid,) + s2)


def _workers_cpu_percent(pids: list[int],
                         interval: float = 0.15) -> Optional[float]:
    """Summed CPU% across workers over one short blocking interval.

    Sampled together rather than per worker, so N workers still cost a single
    `interval`, not N of them.
    """
    before = {p: _proc_cpu_times(p) for p in pids}
    before = {p: s for p, s in before.items() if s is not None}
    if not before:
        return None
    time.sleep(interval)
    prev = tuple((p,) + s for p, s in sorted(before.items()))
    after = {p: _proc_cpu_times(p) for p in before}
    after = {p: s for p, s in after.items() if s is not None}
    if not after:
        return None
    cur = tuple((p,) + s for p, s in sorted(after.items()))
    return _cpu_from_multi(prev, cur)


def _cpu_from_multi(prev, cur) -> Optional[float]:
    """Summed CPU% from two multi-worker sample tuples.

    Only PIDs present in BOTH samples contribute: a worker that started or was
    restarted between polls has no meaningful delta, and counting it would show
    a spike that never happened.
    """
    if not prev or not cur:
        return None
    prev_by_pid = {s[0]: s for s in prev}
    total = 0.0
    seen = False
    for sample in cur:
        old = prev_by_pid.get(sample[0])
        if old is None:
            continue
        pct = _cpu_from_samples(old, sample)
        if pct is not None:
            total += pct
            seen = True
    return total if seen else None


def _cpu_from_samples(prev, cur) -> Optional[float]:
    """CPU% (of one core) from two (pid, proc_jiffies, total_jiffies) samples."""
    if not prev or not cur:
        return None
    # A restarted worker (different pid) resets counters -> no valid delta yet.
    if prev[0] != cur[0]:
        return None
    dproc = cur[1] - prev[1]
    dtotal = cur[2] - prev[2]
    if dtotal <= 0:
        return 0.0
    ncpu = os.cpu_count() or 1
    return max(0.0, min(100.0 * ncpu, 100.0 * ncpu * dproc / dtotal))


def get_status(*, sample_cpu: bool = True, prev_cpu_sample=None) -> PostprocessStatus:
    """Best-effort snapshot of the post-processing pipeline.

    ``prev_cpu_sample`` is an opaque ``(pid, proc_jiffies, total_jiffies)`` tuple
    from a previous call. When provided, worker CPU% is computed as a NON-blocking
    delta against it (no sleep) — ideal for repeated polling from a UI timer. The
    fresh sample for the next call is attached to the returned status as
    ``.cpu_sample``. When omitted and ``sample_cpu`` is true, a short blocking
    sample is taken instead.
    """
    st = PostprocessStatus()

    # Worker processes (independent of RabbitMQ reachability). CPU and memory
    # are summed across every worker: with a dozen running, one worker's usage
    # would badly understate the real load.
    pids = find_worker_pids()
    st.workers = len(pids)
    if pids:
        st.worker_running = True
        st.worker_pids = tuple(pids)
        samples = {p: _proc_cpu_times(p) for p in pids}
        fresh = {p: s for p, s in samples.items() if s is not None}
        if fresh:
            st.cpu_sample = tuple((p,) + s for p, s in sorted(fresh.items()))
        if prev_cpu_sample is not None and st.cpu_sample is not None:
            st.worker_cpu = _cpu_from_multi(prev_cpu_sample, st.cpu_sample)
        elif sample_cpu:
            st.worker_cpu = _workers_cpu_percent(pids)
        rss = [_proc_rss_mb(p) for p in pids]
        rss = [r for r in rss if r is not None]
        st.worker_rss_mb = sum(rss) if rss else None

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
        worker-load: [█████████░] 92% cpu · queue 4 (1 busy) · working
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
        # Total queue length (waiting + in-flight); call out in-flight separately.
        q = f"queue {st.queued}"
        if st.unacked:
            q += f" ({st.unacked} busy)"
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

        workers   12 up
        cpu       [####______]  38%
        mem       675 MB
        queue     4  (1 busy)
        rate      2.2 in / 1.8 out
    """
    # One wider than the longest label ("consumers"), so a value never abuts it.
    label_w = 10
    bar_w = max(6, min(12, width - label_w - 8))

    def row(label: str, value: str) -> str:
        return f"{label:<{label_w}s}{value}"

    lines = []
    if st.worker_running:
        lines.append(row("workers", f"{st.workers} up"))
        cpu = f"{st.worker_cpu:.0f}%" if st.worker_cpu is not None else "--"
        lines.append(row("cpu", f"[{_cpu_bar(st.worker_cpu, bar_w)}] {cpu}"))
        if st.worker_rss_mb is not None:
            lines.append(row("mem", f"{st.worker_rss_mb:.0f} MB"))
    else:
        lines.append(row("workers", "none running"))

    if st.available:
        q = str(st.queued)
        if st.unacked:
            q += f"  ({st.unacked} busy)"
        lines.append(row("queue", q))
        lines.append(row("consumers", str(st.consumers)))
        if st.deliver_rate or st.publish_rate:
            lines.append(row("rate", f"{st.publish_rate:.1f} in / "
                                     f"{st.deliver_rate:.1f} out"))
        lines.append(row("state", "working" if st.busy else "idle"))
    else:
        lines.append(row("broker", "unreachable"))

    return "\n".join(lines)
