import os
from fastapi.testclient import TestClient
from karaoke.api import app

client = TestClient(app)

def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"

def test_stage_whisper_validation():
    # File not found
    res = client.post("/api/staging/whisper", json={
        "file_path": "/does/not/exist/audio.mp3",
        "artist": "Melvins",
        "title": "A History of Bad Men"
    })
    assert res.status_code == 400
    assert "File not found" in res.json()["detail"]

def test_stage_youtube_accepted():
    res = client.post("/api/staging/youtube", json={
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    })
    assert res.status_code == 200
    assert res.json()["status"] == "accepted"
