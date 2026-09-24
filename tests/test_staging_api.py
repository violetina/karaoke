"""Tests for the background staging supervisor API endpoints."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from karaoke import api as api_mod
from karaoke import staging_api


@pytest.fixture()
def client():
    return TestClient(api_mod.app)


def test_staging_jobs_endpoint_exists(client):
    resp = client.get("/api/staging/jobs")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_stage_whisper_rejects_missing_file(client):
    resp = client.post(
        "/api/staging/whisper",
        json={"file_path": "/does/not/exist/audio.mp3", "artist": "Melvins", "title": "Honey Bucket"},
    )
    assert resp.status_code == 400
    assert "File not found" in resp.json()["detail"]


def test_stage_whisper_accepts_and_returns_job_id(client, monkeypatch, tmp_path):
    audio = tmp_path / "track.mp3"
    audio.write_bytes(b"fake audio")

    captured = {}

    def fake_start(file_path, artist, title):
        captured["args"] = (file_path, artist, title)
        return "whisper-test-1"

    monkeypatch.setattr(staging_api.supervisor, "start_whisper_job", fake_start)

    resp = client.post(
        "/api/staging/whisper",
        json={"file_path": str(audio), "artist": "Melvins", "title": "Honey Bucket"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["job_id"] == "whisper-test-1"
    assert captured["args"] == (str(audio), "Melvins", "Honey Bucket")


def test_stage_youtube_accepts_and_returns_job_id(client, monkeypatch):
    def fake_start(url):
        return "youtube-test-1"

    monkeypatch.setattr(staging_api.supervisor, "start_youtube_job", fake_start)

    resp = client.post(
        "/api/staging/youtube",
        json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["job_id"] == "youtube-test-1"


def test_get_job_404_for_unknown(client):
    resp = client.get("/api/staging/jobs/nope-123")
    assert resp.status_code == 404
