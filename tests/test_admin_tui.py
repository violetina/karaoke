"""Tests for the Karaoke Backend Operations / Admin TUI."""
from __future__ import annotations

from karaoke.admin_tui import KaraokeAdminApp


def test_admin_app_initialization(monkeypatch):
    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    app._target_workers = 1
    assert app.TITLE == "Karaoke Platform Operations & Worker Control"
    assert app._target_workers == 1


def test_admin_app_scaling_actions(monkeypatch):
    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    app._target_workers = 2
    notifications = []
    monkeypatch.setattr(app, "notify", lambda msg, **k: notifications.append(msg), raising=False)

    calls = []
    class FakeApi:
        ctrl_url = "http://127.0.0.1:8765"
        def _http_post(self, url, path, body):
            calls.append((path, body))
            return {"status": "ok", "target": body.get("target")}

    monkeypatch.setattr(app, "api", FakeApi(), raising=False)

    # 1. Scale Up
    app.action_scale_up()
    assert app._target_workers == 3
    assert calls[0] == ("/api/workers/scale", {"target": 3})

    # 2. Scale Down
    app.action_scale_down()
    assert app._target_workers == 2
    assert calls[1] == ("/api/workers/scale", {"target": 2})


def test_admin_pipeline_actions_dispatch(monkeypatch):
    """The audio-processing controls launch background workers without raising."""
    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
    notifications = []
    monkeypatch.setattr(app, "notify", lambda msg, **k: notifications.append(msg), raising=False)

    started = []
    monkeypatch.setattr(app, "run_worker", lambda fn, **k: started.append(fn), raising=False)

    app.action_run_backfill()
    app.action_rebuild_vectors()
    app.action_analyse_recordings()

    # Each control notified the user and queued exactly one background worker.
    assert len(started) == 3
    assert all(callable(fn) for fn in started)
    assert len(notifications) == 3


def test_admin_whisper_align_dispatch(monkeypatch):
    """Whisper alignment control dispatches background worker when inputs are given."""
    app = KaraokeAdminApp.__new__(KaraokeAdminApp)
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

    app.action_align_whisper()
    assert len(started) == 1
    assert callable(started[0])
    assert "Aligning lyrics for '62'" in notifications[0]
