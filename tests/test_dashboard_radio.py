from subprocess import CompletedProcess

from fastapi.testclient import TestClient

from karaoke import dashboard_api as api


client = TestClient(api.app)


def test_radio_status_reports_latest_song(monkeypatch):
    monkeypatch.setattr(
        api.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args[0], 0, stdout="active\n", stderr=""),
    )
    monkeypatch.setattr(
        "karaoke.live_state.read",
        lambda source: {"artist": "Burial", "title": "Archangel"} if source == "radio" else None,
    )

    response = client.get("/api/radio")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "status": "recognized",
        "artist": "Burial",
        "title": "Archangel",
        "processing": "Full processing is queued automatically for every recognized song",
    }


def test_radio_toggle_starts_user_unit(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if "is-active" in command:
            return CompletedProcess(command, 0, stdout="active\n", stderr="")
        return CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(api.subprocess, "run", run)
    monkeypatch.setattr("karaoke.live_state.read", lambda source: None)

    response = client.post("/api/radio", json={"enabled": True})

    assert response.status_code == 200
    assert response.json()["status"] == "listening"
    assert commands[0] == ["systemctl", "--user", "start", "karaoke-radio.service"]


def test_radio_toggle_surfaces_systemd_failure(monkeypatch):
    monkeypatch.setattr(
        api.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args[0], 5, stdout="", stderr="unit failed"),
    )

    response = client.post("/api/radio", json={"enabled": True})

    assert response.status_code == 503
    assert response.json()["detail"] == "unit failed"
