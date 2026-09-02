"""Tests for the split library API and host-side control API."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from karaoke import api as api_mod
from karaoke import ctrl_api as ctrl_mod
from karaoke import localcache

# Capture the real connect() before monkeypatching to avoid infinite recursion.
_real_connect = localcache.connect


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Point the API at a throwaway SQLite database seeded with one track."""
    db_path = tmp_path / "karaoke.db"
    monkeypatch.setattr(
        "karaoke.api.localcache.connect",
        lambda db_p=None: _real_connect(db_p or db_path),
    )

    conn = _real_connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tracks (artist, title, album, duration) VALUES (?, ?, ?, ?)",
        ("Radiohead", "Creep", "Pablo Honey", 238.0),
    )
    track_id = cur.lastrowid
    cur.execute(
        "INSERT INTO sources (track_id, url, kind) VALUES (?, ?, ?)",
        (track_id, "https://youtu.be/XFkzRNyygfk", "youtube"),
    )
    conn.commit()
    conn.close()
    return db_path, track_id


@pytest.fixture()
def client():
    return TestClient(api_mod.app)


def test_health_reports_ok_and_version(db, client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["version"] == api_mod.API_VERSION


def test_health_alias_under_api_prefix(db, client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_list_tracks_returns_seeded_track(db, client):
    resp = client.get("/api/tracks")
    assert resp.status_code == 200
    tracks = resp.json()
    assert len(tracks) == 1
    assert tracks[0]["artist"] == "Radiohead"
    assert tracks[0]["title"] == "Creep"
    assert tracks[0]["kind"] == "youtube"
    assert tracks[0]["has_synced_lyrics"] is False


def test_list_tracks_search_filter(db, client):
    assert len(client.get("/api/tracks", params={"q": "radio"}).json()) == 1
    assert client.get("/api/tracks", params={"q": "nosuchband"}).json() == []


def test_list_tracks_respects_limit(db, client):
    resp = client.get("/api/tracks", params={"limit": 1})
    assert resp.status_code == 200
    assert len(resp.json()) <= 1


def test_get_track_detail_includes_sources(db, client):
    _, track_id = db
    resp = client.get(f"/api/tracks/{track_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Creep"
    assert body["sources"][0]["url"] == "https://youtu.be/XFkzRNyygfk"


def test_get_track_missing_returns_404(db, client):
    assert client.get("/api/tracks/999999").status_code == 404


def test_library_api_exposes_no_playback_route(db, client):
    """Playback is desktop-bound and must not ship in the deployable image."""
    assert client.post("/api/play", json={"url": "https://example.com"}).status_code == 404
    routes = {getattr(r, "path", None) for r in api_mod.app.routes}
    assert "/api/play" not in routes


def test_library_api_does_not_import_desktop_opener():
    """Guard against re-introducing an xdg-open dependency into the image."""
    assert not hasattr(api_mod, "open_song_url")


def test_deployable_modules_do_not_require_textual():
    """api/ctrl_api must import without the TUI stack installed.

    Regression: ctrl_api imported open_song_url from browse.py, which pulls in
    textual at module level, so CI (no textual) failed at collection while a
    dev machine with textual passed.
    """
    import subprocess
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    script = (
        "import sys;"
        "sys.modules['textual'] = None;"
        "import karaoke.api, karaoke.ctrl_api;"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env={"PYTHONPATH": str(src), "PATH": ""},
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# --- control API -------------------------------------------------------

def test_ctrl_health_reports_control_role():
    body = TestClient(ctrl_mod.app).get("/health").json()
    assert body["status"] == "ok"
    assert body["role"] == "control"


def test_ctrl_play_launches_url(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "karaoke.ctrl_api.open_song_url",
        lambda url, kind: calls.append((url, kind)) or 4242,
    )
    resp = TestClient(ctrl_mod.app).post(
        "/api/play", json={"url": "https://youtu.be/abc", "kind": "youtube"}
    )
    assert resp.status_code == 200
    assert resp.json()["pid"] == 4242
    assert calls == [("https://youtu.be/abc", "youtube")]


def test_ctrl_play_falls_back_to_youtube_search(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "karaoke.ctrl_api.open_song_url",
        lambda url, kind: captured.update(url=url, kind=kind) or 1,
    )
    resp = TestClient(ctrl_mod.app).post(
        "/api/play", json={"artist": "Radiohead", "title": "Creep"}
    )
    assert resp.status_code == 200
    assert captured["kind"] == "youtube_search"
    assert "Radiohead" in captured["url"]


def test_ctrl_play_requires_some_input():
    assert TestClient(ctrl_mod.app).post("/api/play", json={}).status_code == 400


def test_ctrl_play_surfaces_launch_failure(monkeypatch):
    def boom(url, kind):
        raise RuntimeError("no display")

    monkeypatch.setattr("karaoke.ctrl_api.open_song_url", boom)
    resp = TestClient(ctrl_mod.app).post(
        "/api/play", json={"url": "https://youtu.be/abc"}
    )
    assert resp.status_code == 500
