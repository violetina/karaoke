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


def test_staging_queue_lists_pending_items(client, monkeypatch):
    from karaoke.staging import StagedLyrics

    fake_item = StagedLyrics(
        id=7, artist="The Cure", title="A Forest", album="", duration=None,
        source_kind="youtube_caption", source_url="https://youtu.be/x", confidence=0.65,
        status="pending", plain_lyrics="Come closer", synced_lyrics="", notes="",
        created_at=1.0, updated_at=1.0,
    )
    captured = {}

    def fake_list_staged(*, status="pending", limit=20, conn=None):
        captured["args"] = (status, limit)
        return [fake_item]

    monkeypatch.setattr(staging_api.staging, "list_staged", fake_list_staged)

    resp = client.get("/api/staging/queue?status=pending&limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == 7
    assert body["items"][0]["has_synced"] is False
    assert captured["args"] == ("pending", 10)


def test_staging_approve_returns_updated_item(client, monkeypatch):
    from karaoke.staging import StagedLyrics

    approved_item = StagedLyrics(
        id=7, artist="The Cure", title="A Forest", album="", duration=None,
        source_kind="youtube_caption", source_url="", confidence=0.65,
        status="approved", plain_lyrics="Come closer", synced_lyrics="", notes="",
        created_at=1.0, updated_at=2.0,
    )
    monkeypatch.setattr(staging_api.staging, "approve_staged", lambda item_id: approved_item)

    resp = client.post("/api/staging/queue/7/approve")
    assert resp.status_code == 200
    assert resp.json()["item"]["status"] == "approved"


def test_staging_approve_404_for_unknown_item(client, monkeypatch):
    def fake_approve(item_id):
        raise KeyError(f"no staged lyrics with id {item_id}")

    monkeypatch.setattr(staging_api.staging, "approve_staged", fake_approve)

    resp = client.post("/api/staging/queue/999/approve")
    assert resp.status_code == 404


def test_staging_reject_returns_updated_item(client, monkeypatch):
    from karaoke.staging import StagedLyrics

    rejected_item = StagedLyrics(
        id=8, artist="A", title="B", album="", duration=None,
        source_kind="web", source_url="", confidence=0.4,
        status="rejected", plain_lyrics="candidate", synced_lyrics="", notes="",
        created_at=1.0, updated_at=2.0,
    )
    monkeypatch.setattr(staging_api.staging, "reject_staged", lambda item_id: rejected_item)

    resp = client.post("/api/staging/queue/8/reject")
    assert resp.status_code == 200
    assert resp.json()["item"]["status"] == "rejected"
