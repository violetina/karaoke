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
    with patch("karaoke.api_client.ApiClient._http_post", return_value={"status": "launched", "url": "http://x"}):
        res = client.play(artist="Portishead", title="Glory Box")
        assert res["status"] == "launched"
        assert res["url"] == "http://x"


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
