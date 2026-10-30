"""Dashboard queue-follower control endpoints (/api/follow)."""
from subprocess import CompletedProcess

from fastapi.testclient import TestClient

from karaoke import dashboard_api as api


client = TestClient(api.app)


_QUEUE = {
    "playlist_id": "p1",
    "name": "Smart Genre Hip Hop",
    "current_index": 2,
    "rows": [
        {"artist": "A", "title": "One"},
        {"artist": "B", "title": "Two"},
        {"artist": "C", "title": "Three"},
        {"artist": "D", "title": "Four"},
    ],
}


def test_follow_status_reports_current_row(monkeypatch):
    monkeypatch.setattr(
        api.subprocess, "run",
        lambda *a, **k: CompletedProcess(a[0], 0, stdout="active\n", stderr=""),
    )
    monkeypatch.setattr("karaoke.localcache.load_active_queue", lambda: _QUEUE)

    data = client.get("/api/follow").json()
    assert data["enabled"] is True
    assert data["status"] == "following"
    assert data["index"] == 2
    assert data["total"] == 4
    assert data["current"] == {"artist": "C", "title": "Three"}


def test_follow_toggle_starts_user_unit(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if "is-active" in command:
            return CompletedProcess(command, 0, stdout="active\n", stderr="")
        return CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(api.subprocess, "run", run)
    monkeypatch.setattr("karaoke.localcache.load_active_queue", lambda: _QUEUE)

    res = client.post("/api/follow", json={"enabled": True})
    assert res.status_code == 200
    assert res.json()["enabled"] is True
    assert commands[0] == ["systemctl", "--user", "start", "karaoke-queue-follow.service"]


def test_follow_toggle_surfaces_systemd_failure(monkeypatch):
    monkeypatch.setattr(
        api.subprocess, "run",
        lambda *a, **k: CompletedProcess(a[0], 5, stdout="", stderr="unit failed"),
    )
    monkeypatch.setattr("karaoke.localcache.load_active_queue", lambda: _QUEUE)

    res = client.post("/api/follow", json={"enabled": True})
    assert res.status_code == 503
    assert res.json()["detail"] == "unit failed"


def test_follow_status_reports_autopilot(monkeypatch):
    monkeypatch.setattr(
        api.subprocess, "run",
        lambda *a, **k: CompletedProcess(a[0], 0, stdout="active\n", stderr=""),
    )
    q = dict(_QUEUE, autopilot=True)
    monkeypatch.setattr("karaoke.localcache.load_active_queue", lambda: q)

    assert client.get("/api/follow").json()["autopilot"] is True


def test_autopilot_toggle_persists_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr("karaoke.localcache.set_queue_autopilot",
                        lambda enabled, **k: seen.update(enabled=enabled))

    res = client.post("/api/queue/autopilot", json={"enabled": True})
    assert res.status_code == 200
    assert res.json()["autopilot"] is True
    assert seen["enabled"] is True

