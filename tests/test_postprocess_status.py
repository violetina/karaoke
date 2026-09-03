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
    assert "queue 3" in line
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
