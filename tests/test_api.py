"""Tests for the FastAPI backend endpoints."""
from fastapi.testclient import TestClient

from karaoke.api import app
from karaoke import localcache
from karaoke.lyrics import Lyrics

client = TestClient(app)
_real_connect = localcache.connect


def test_health_check():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    response_api = client.get("/api/health")
    assert response_api.status_code == 200
    assert response_api.json() == {"status": "ok"}


def test_list_tracks(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    conn = _real_connect(db_path)
    localcache.add_track_and_lyrics(
        "Radiohead", "Creep", Lyrics(plain="I'm a creep"), url="https://youtube.com/watch?v=1", kind="youtube", conn=conn
    )
    localcache.add_track_and_lyrics(
        "Nirvana", "Smells Like Teen Spirit", Lyrics(plain="Hello"), url="https://youtube.com/watch?v=2", kind="youtube", conn=conn
    )

    monkeypatch.setattr("karaoke.api.localcache.connect", lambda db_p=None: _real_connect(db_p or db_path))

    res = client.get("/api/tracks")
    assert res.status_code == 200
    tracks = res.json()
    assert len(tracks) == 2
    assert tracks[0]["artist"] == "Nirvana"
    assert tracks[1]["artist"] == "Radiohead"

    res_q = client.get("/api/tracks?q=Creep")
    assert res_q.status_code == 200
    q_tracks = res_q.json()
    assert len(q_tracks) == 1
    assert q_tracks[0]["title"] == "Creep"


def test_get_track_details(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    conn = _real_connect(db_path)
    localcache.add_track_and_lyrics(
        "Blur", "Song 2", Lyrics(plain="Woo-hoo!"), url="https://youtube.com/watch?v=blur", kind="youtube", conn=conn
    )
    track_id = localcache.find_track_id("Blur", "Song 2", conn)

    monkeypatch.setattr("karaoke.api.localcache.connect", lambda db_p=None: _real_connect(db_p or db_path))

    res = client.get(f"/api/tracks/{track_id}")
    assert res.status_code == 200
    data = res.json()
    assert data["artist"] == "Blur"
    assert data["title"] == "Song 2"
    assert len(data["sources"]) == 1
    assert data["sources"][0]["url"] == "https://youtube.com/watch?v=blur"
    assert data["lyrics"]["plain"] == "Woo-hoo!"


def test_get_track_not_found(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    _real_connect(db_path)
    monkeypatch.setattr("karaoke.api.localcache.connect", lambda db_p=None: _real_connect(db_p or db_path))

    res = client.get("/api/tracks/99999")
    assert res.status_code == 404


def test_play_track_endpoint(monkeypatch):
    monkeypatch.setattr("karaoke.api.open_song_url", lambda url, kind: 1234)

    res = client.post("/api/play", json={"url": "https://youtube.com/watch?v=test", "kind": "youtube"})
    assert res.status_code == 200
    payload = res.json()
    assert payload["status"] == "launched"
    assert payload["pid"] == 1234
    assert payload["url"] == "https://youtube.com/watch?v=test"


def test_play_track_fallback_search(monkeypatch):
    monkeypatch.setattr("karaoke.api.open_song_url", lambda url, kind: 5678)

    res = client.post("/api/play", json={"artist": "Pixies", "title": "Where Is My Mind?"})
    assert res.status_code == 200
    payload = res.json()
    assert payload["kind"] == "youtube_search"
    assert payload["pid"] == 5678
    assert "Pixies" in payload["url"]


def test_get_stats_endpoint(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    conn = _real_connect(db_path)
    localcache.log_event("spotify", "play", artist="Queen", title="Bohemian Rhapsody", conn=conn)

    monkeypatch.setattr("karaoke.api.localcache.connect", lambda db_p=None: _real_connect(db_p or db_path))
    monkeypatch.setattr("karaoke.localcache.connect", lambda db_p=None: _real_connect(db_p or db_path))

    res = client.get("/api/stats")
    assert res.status_code == 200
    data = res.json()
    assert "total_events" in data
    assert "plays" in data


def test_get_logs_endpoint(tmp_path, monkeypatch):
    log_file = tmp_path / "test.log"
    log_file.write_text("line 1\nline 2\nline 3\n")
    monkeypatch.setattr("karaoke.api.LOG_FILE", log_file)

    res = client.get("/api/logs?lines=2")
    assert res.status_code == 200
    data = res.json()
    assert data["lines"] == ["line 2", "line 3"]
