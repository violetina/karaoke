"""Tests for the Karaoke Backend Operations / Admin TUI."""
from __future__ import annotations

from karaoke.admin_tui import KaraokeAdminApp


class _FakeLock:
    def __init__(self, name: str):
        self.name = name
        self.held = False

    def acquire(self, *, blocking: bool = False) -> bool:
        self.held = True
        return True

    def release(self) -> None:
        self.held = False


def _fake_pipeline_lock(monkeypatch):
    monkeypatch.setattr("karaoke.lockfile.ProcessLock", _FakeLock)


def test_admin_app_initialization(monkeypatch):
    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    app._target_workers = 1
    assert app.TITLE == "Karaoke Platform Operations & Worker Control"
    assert app._target_workers == 1


def test_admin_app_scaling_actions(monkeypatch):
    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    app._target_workers = 0
    notifications = []
    monkeypatch.setattr(app, "notify", lambda msg, **k: notifications.append(msg), raising=False)

    calls = []
    class FakeApi:
        ctrl_url = "http://127.0.0.1:8765"
        def _http_post(self, url, path, body):
            calls.append((path, body))
            return {"status": "ok", "target": body.get("target")}

    monkeypatch.setattr(app, "api", FakeApi(), raising=False)

    # 1. Start worker
    app.action_scale_up()
    assert app._target_workers == 1
    assert calls[0] == ("/api/workers/scale", {"target": 1})

    # 2. Stop worker
    app.action_scale_down()
    assert app._target_workers == 0
    assert calls[1] == ("/api/workers/scale", {"target": 0})


def test_admin_pipeline_actions_dispatch(monkeypatch):
    """The audio-processing controls launch background workers without raising."""
    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    app._bg_busy = False
    app._bg_lock = None
    _fake_pipeline_lock(monkeypatch)
    notifications = []
    monkeypatch.setattr(app, "notify", lambda msg, **k: notifications.append(msg), raising=False)

    started = []
    monkeypatch.setattr(app, "run_worker", lambda fn, **k: started.append(fn), raising=False)

    app.action_run_backfill()
    app._release_bg_lock()
    app.action_rebuild_vectors()
    app._release_bg_lock()
    app.action_analyse_recordings()
    app._release_bg_lock()

    # Each control notified the user and queued exactly one background worker.
    assert len(started) == 3
    assert all(callable(fn) for fn in started)
    assert len(notifications) == 3


def test_admin_whisper_align_dispatch(monkeypatch):
    """Whisper alignment control dispatches background worker when inputs are given."""
    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    app._bg_busy = False
    app._bg_lock = None
    _fake_pipeline_lock(monkeypatch)
    notifications = []
    monkeypatch.setattr(app, "notify", lambda msg, **k: notifications.append(msg), raising=False)

    started = []
    monkeypatch.setattr(app, "run_worker", lambda fn, **k: started.append(fn), raising=False)

    class FakeInput:
        def __init__(self, val):
            self.value = val

    def fake_query(selector, *a, **k):
        if selector == "#align-track-input":
            return FakeInput("62")
        return FakeInput("Some lyrics text")

    monkeypatch.setattr(app, "query_one", fake_query, raising=False)

    try:
        app.action_align_whisper()
        assert len(started) == 1
        assert callable(started[0])
        assert "Aligning lyrics for '62'" in notifications[0]
    finally:
        app._release_bg_lock()


def test_admin_single_operation_guard(monkeypatch):
    """A second audio task is refused while one is already running."""
    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    app._bg_busy = False
    app._bg_lock = None
    _fake_pipeline_lock(monkeypatch)
    notifications = []
    monkeypatch.setattr(app, "notify", lambda msg, **k: notifications.append((msg, k)), raising=False)

    started = []
    monkeypatch.setattr(app, "run_worker", lambda fn, **k: started.append(fn), raising=False)

    try:
        # First rebuild acquires the lock and dispatches a worker.
        app.action_rebuild_vectors()
        assert len(started) == 1
        assert app._bg_busy is True

        # Second rebuild is refused (lock held) — no new worker, a warning fires.
        app.action_rebuild_vectors()
        assert len(started) == 1
        assert any(k.get("severity") == "warning" for _, k in notifications)
    finally:
        app._release_bg_lock()
