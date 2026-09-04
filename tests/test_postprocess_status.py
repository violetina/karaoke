"""Tests for the post-processing status probe's pure formatting helpers."""
from karaoke.postprocess_status import (
    PostprocessStatus,
    _cpu_bar,
    worker_load_line,
)


def test_cpu_bar_scales_with_percent():
    assert _cpu_bar(0, width=10) == "░" * 10
    assert _cpu_bar(100, width=10) == "█" * 10
    assert _cpu_bar(50, width=10) == "█" * 5 + "░" * 5


def test_cpu_bar_none_is_dashes():
    assert _cpu_bar(None, width=6) == "─" * 6


def test_busy_property():
    assert PostprocessStatus(ready=2).busy
    assert PostprocessStatus(unacked=1).busy
    assert not PostprocessStatus(ready=0, unacked=0).busy


def test_queued_totals_ready_and_unacked():
    assert PostprocessStatus(ready=3, unacked=1).queued == 4
    assert PostprocessStatus(ready=0, unacked=0).queued == 0


def test_cpu_from_samples_delta():
    from karaoke.postprocess_status import _cpu_from_samples
    # 50 proc jiffies out of 100 total, on a hypothetical single-core view.
    import karaoke.postprocess_status as ps
    # pid must match between samples.
    prev = (99, 1000, 10000)
    cur = (99, 1050, 10100)  # dproc=50 dtotal=100 -> 50% * ncpu
    val = _cpu_from_samples(prev, cur)
    assert val is not None and val > 0


def test_cpu_from_samples_pid_change_returns_none():
    from karaoke.postprocess_status import _cpu_from_samples
    assert _cpu_from_samples((1, 100, 1000), (2, 200, 2000)) is None


def test_worker_load_line_idle():
    st = PostprocessStatus(
        available=True, worker_running=True, worker_cpu=0.0, ready=0, unacked=0
    )
    line = worker_load_line(st)
    assert line.startswith("worker-load:")
    assert "0% cpu" in line
    assert "idle" in line


def test_worker_load_line_working_shows_busy_and_queue():
    st = PostprocessStatus(
        available=True, worker_running=True, worker_cpu=92.0, ready=3, unacked=1
    )
    line = worker_load_line(st)
    assert "92% cpu" in line
    assert "queue 4" in line   # 3 ready + 1 in-flight
    assert "1 busy" in line
    assert "working" in line


def test_worker_load_line_worker_down():
    st = PostprocessStatus(available=False, worker_running=False)
    line = worker_load_line(st)
    assert "worker down" in line
    assert "broker unreachable" in line


def test_worker_load_line_worker_up_broker_down():
    st = PostprocessStatus(available=False, worker_running=True, worker_cpu=5.0)
    line = worker_load_line(st)
    assert "5% cpu" in line
    assert "broker unreachable" in line


# --- multi-worker awareness ------------------------------------------------

def _st(**kw):
    from karaoke.postprocess_status import PostprocessStatus
    base = dict(available=True, worker_running=True, workers=12,
                worker_cpu=38.0, worker_rss_mb=675.0, ready=4, unacked=1,
                consumers=12, deliver_rate=2.2, publish_rate=1.8)
    base.update(kw)
    return PostprocessStatus(**base)


def test_worker_panel_labels_and_values_never_abut():
    """"consumers" is 9 chars; a 9-wide column produced "consumers12"."""
    from karaoke.postprocess_status import worker_panel

    for line in worker_panel(_st()).splitlines():
        label = line.split()[0]
        assert line[len(label)] == " ", line


def test_worker_panel_values_start_in_one_column():
    from karaoke.postprocess_status import worker_panel

    starts = {len(l) - len(l.lstrip()) or l.index(l.split()[1])
              for l in worker_panel(_st()).splitlines() if len(l.split()) > 1}
    assert len(starts) == 1


def test_worker_panel_reports_the_worker_count():
    from karaoke.postprocess_status import worker_panel
    assert "12 up" in worker_panel(_st())


def test_worker_panel_shows_queue_and_busy():
    from karaoke.postprocess_status import worker_panel
    panel = worker_panel(_st())
    assert "5" in panel                 # queued = ready + unacked
    assert "1 busy" in panel
    assert "working" in panel


def test_worker_panel_when_nothing_is_running():
    from karaoke.postprocess_status import worker_panel
    panel = worker_panel(_st(worker_running=False, workers=0, available=False))
    assert "none running" in panel
    assert "unreachable" in panel


def test_worker_panel_is_ascii_before_the_bar():
    """Same rule as the sentiment bars: nothing mis-measurable in the labels."""
    from karaoke.postprocess_status import worker_panel
    for line in worker_panel(_st()).splitlines():
        assert line[:10].isascii()


def test_cpu_and_memory_are_summed_across_workers(monkeypatch):
    """One worker's usage would understate a twelve-worker fleet."""
    from karaoke import postprocess_status as ps

    monkeypatch.setattr(ps, "find_worker_pids", lambda: [101, 102, 103])
    monkeypatch.setattr(ps, "_proc_rss_mb", lambda pid: 100.0)
    monkeypatch.setattr(ps, "_proc_cpu_times", lambda pid: (10, 1000))
    monkeypatch.setattr(ps, "_fetch_queue", lambda *a, **k: {})

    st = ps.get_status(sample_cpu=False)
    assert st.workers == 3
    assert st.worker_rss_mb == 300.0
    assert st.worker_pids == (101, 102, 103)


def test_a_restarted_worker_does_not_spike_cpu():
    """A pid absent from the previous sample has no delta and is skipped."""
    from karaoke.postprocess_status import _cpu_from_multi

    prev = ((1, 100, 10000),)
    cur = ((1, 110, 20000), (2, 5000, 20000))     # pid 2 is new
    assert _cpu_from_multi(prev, cur) is not None
