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
