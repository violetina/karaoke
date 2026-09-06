"""Tests for the cross-process ProcessLock."""
from __future__ import annotations

from karaoke.lockfile import ProcessLock


def test_process_lock_is_exclusive():
    a = ProcessLock("unit_test_lock")
    b = ProcessLock("unit_test_lock")
    try:
        assert a.acquire() is True
        # A second lock on the same name (separate fd) is refused.
        assert b.acquire() is False
    finally:
        a.release()
    # Once released, it can be acquired again.
    try:
        assert b.acquire() is True
    finally:
        b.release()


def test_process_lock_context_manager():
    with ProcessLock("unit_test_lock_ctx") as acquired:
        assert acquired is True
    # After the context exits the lock is free.
    lock = ProcessLock("unit_test_lock_ctx")
    try:
        assert lock.acquire() is True
    finally:
        lock.release()
