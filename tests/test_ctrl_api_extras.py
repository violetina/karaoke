"""API surface for player controls, folder scan, and audio cutting.

These endpoints live in the host-side control API (karaoke.ctrl_api) because
they need playerctl / ffmpeg / songrec — a future web UI drives them over HTTP
exactly as the TUI drives the same underlying functions.
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


@pytest.fixture()
def ctrl():
    from karaoke.ctrl_api import app
    return TestClient(app)


# -- player listing & current --------------------------------------------

def test_players_list(ctrl):
    with patch("karaoke.playerctl.list_players", return_value=["firefox", "spotify"]), \
         patch("karaoke.playerctl.playing_players", return_value=["spotify"]), \
         patch("karaoke.playerctl.playing_player", return_value="spotify"):
        body = ctrl.get("/api/players").json()
        assert body["players"] == ["firefox", "spotify"]
        assert body["playing"] == ["spotify"]
        assert body["active"] == "spotify"
        assert body["count"] == 2


def test_player_current(ctrl):
    meta = MagicMock(artist="Portishead", title="Glory Box", album="Dummy",
                     url="http://x", player="Spotify", mpris_name="spotify",
                     duration=300.0)
    with patch("karaoke.playerctl.playing_player", return_value="spotify"), \
         patch("karaoke.playerctl.current_metadata", return_value=meta), \
         patch("karaoke.playerctl.status", return_value="Playing"), \
         patch("karaoke.playerctl.position", return_value=42.0), \
         patch("karaoke.playerctl.art_url", return_value=None):
        body = ctrl.get("/api/players/current").json()
        assert body["player"] == "spotify"
        assert body["status"] == "Playing"
        assert body["position_s"] == 42.0
        assert body["metadata"]["artist"] == "Portishead"
        assert body["metadata"]["title"] == "Glory Box"


# -- player control actions ----------------------------------------------

def test_player_play_pause(ctrl):
    with patch("karaoke.playerctl.play_pause", return_value=True):
        body = ctrl.post("/api/players/play-pause", json={"player": "spotify"}).json()
        assert body["status"] == "ok"
        assert body["action"] == "play-pause"


def test_player_next_no_player_is_409(ctrl):
    with patch("karaoke.playerctl.next_track", return_value=False):
        resp = ctrl.post("/api/players/next", json={})
        assert resp.status_code == 409


def test_player_seek(ctrl):
    with patch("karaoke.playerctl.seek", return_value=True):
        body = ctrl.post("/api/players/seek", json={"offset_s": 5.0}).json()
        assert body["status"] == "ok"
        assert body["offset_s"] == 5.0


# -- folder scan ----------------------------------------------------------

def test_scan_folder_missing_dir_is_400(ctrl):
    resp = ctrl.post("/api/scan/folder", json={"dir": "/nonexistent/xyz"})
    assert resp.status_code == 400


def test_scan_folder_dry_run_preview(ctrl, tmp_path):
    fake_stats = {"seen": 2, "processed": 2, "fingerprinted": 0,
                  "classified": 2, "sourced": 1, "errors": 0, "items": []}
    with patch("karaoke.folder_scan.scan_and_ingest_folder", return_value=fake_stats):
        body = ctrl.post("/api/scan/folder",
                         json={"dir": str(tmp_path), "dry_run": True}).json()
        assert body["status"] == "preview"
        assert body["seen"] == 2
        assert body["processed"] == 2


def test_scan_folder_real_is_accepted(ctrl, tmp_path):
    with patch("karaoke.folder_scan.scan_and_ingest_folder", return_value={}):
        body = ctrl.post("/api/scan/folder",
                         json={"dir": str(tmp_path), "dry_run": False}).json()
        assert body["status"] == "accepted"


# -- audio cut ------------------------------------------------------------

def test_audio_cut_missing_file_is_400(ctrl):
    resp = ctrl.post("/api/audio/cut",
                     json={"file_path": "/nope.mp3", "duration_s": 10.0})
    assert resp.status_code == 400


def test_audio_cut_success(ctrl, tmp_path):
    src = tmp_path / "in.wav"
    src.write_bytes(b"dummy")
    out = tmp_path / "out.wav"

    def fake_run(cmd, **kwargs):
        out.write_bytes(b"cutcutcut")
        return MagicMock(returncode=0)

    with patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
         patch("subprocess.run", side_effect=fake_run):
        body = ctrl.post("/api/audio/cut", json={
            "file_path": str(src), "start_s": 5.0,
            "duration_s": 10.0, "output_path": str(out),
        }).json()
        assert body["status"] == "ok"
        assert body["output_path"] == str(out)
        assert body["bytes"] > 0
