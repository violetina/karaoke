import pytest
from unittest.mock import patch, MagicMock
from karaoke.api_client import ApiClient


@pytest.fixture()
def client():
    return ApiClient(lib_url="http://127.0.0.1:8000", ctrl_url="http://127.0.0.1:8765", fallback_local=True)


def test_client_fallback_play(client):
    with patch("karaoke.api_client.ApiClient._http_post", return_value=None), \
         patch("karaoke.ctrl_api.play_track", return_value={"status": "launched"}):
        res = client.play(artist="Portishead", title="Glory Box")
        assert res["status"] == "launched"


def test_client_http_play(client):
    seen = {}
    def fake_post(base, path, body=None):
        seen.update(body or {})
        return {"status": "launched", "url": "http://x", "session_id": "play_1"}

    with patch("karaoke.api_client.ApiClient._http_post", side_effect=fake_post):
        res = client.play(artist="Portishead", title="Glory Box")
        assert res["status"] == "launched"
        assert res["url"] == "http://x"
        assert res["session_id"] == "play_1"
        assert seen["prefer_audio"] is True


def test_client_player_controls(client):
    with patch("karaoke.api_client.ApiClient._http_post", return_value={"status": "ok"}):
        assert client.player_play_pause("spotify") is True
        assert client.player_next("spotify") is True
        assert client.player_previous("spotify") is True
        assert client.player_seek(5.0, "spotify") is True


def test_client_scan_folder_http(client):
    fake_stats = {"status": "preview", "seen": 5, "processed": 5}
    with patch("karaoke.api_client.ApiClient._http_post", return_value=fake_stats):
        res = client.scan_folder("/tmp/music", dry_run=True)
        assert res["status"] == "preview"
        assert res["seen"] == 5


def test_client_list_recordings_fallback(client):
    with patch("karaoke.api_client.ApiClient._http_get", return_value=None), \
         patch("karaoke.api.list_recordings", return_value={"recordings": [], "count": 0}):
        res = client.list_recordings()
        assert res["count"] == 0


def test_client_get_stats_http(client):
    fake = {"plays": 5, "total_events": 5, "top_tracks": []}
    with patch("karaoke.api_client.ApiClient._http_get", return_value=fake):
        res = client.get_stats(limit=5, days=7)
        assert res["plays"] == 5


def test_client_list_tracks_http(client):
    with patch("karaoke.api_client.ApiClient._http_get",
               return_value=[{"artist": "A", "title": "B"}]):
        res = client.list_tracks(limit=10)
        assert res[0]["artist"] == "A"


def test_client_list_tracks_empty_on_failure(client):
    with patch("karaoke.api_client.ApiClient._http_get", return_value=None):
        assert client.list_tracks() == []


def test_client_record_analyse_fallback_runs_sync(client):
    with patch("karaoke.api_client.ApiClient._http_post", return_value=None), \
         patch("karaoke.recording_worker.analyse", return_value=["ok  A - B"]):
        res = client.record_analyse(7, keep=True)
        assert res["status"] == "analysed"
        assert res["lines"] == ["ok  A - B"]


def test_client_record_discard_http(client):
    with patch("karaoke.api_client.ApiClient._http_delete",
               return_value={"status": "discarded", "freed_bytes": 1000}):
        res = client.record_discard_audio(3)
        assert res["freed_bytes"] == 1000


def test_client_play_sessions_http(client):
    with patch("karaoke.api_client.ApiClient._http_get",
               return_value={"sessions": [{"session_id": "play_1"}], "count": 1}):
        assert client.list_play_sessions()["count"] == 1
    with patch("karaoke.api_client.ApiClient._http_delete",
               return_value={"session_id": "play_1", "status": "stopped"}):
        assert client.stop_play_session("play_1")["status"] == "stopped"


def test_client_sample_stream_url(client):
    url = client.sample_stream_url(artist="A B", title="C", seconds=20)
    assert url == "http://127.0.0.1:8765/api/sample/stream?artist=A+B&title=C&seconds=20"
