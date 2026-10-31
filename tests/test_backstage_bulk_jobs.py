"""Backstage bulk-processing endpoints: playlist bulk-process and the
library gap-fill background job (wiring scripts/enqueue_playlists.py and
scripts/fill_analysis_and_vector_gaps.py into the dashboard)."""
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from karaoke import dashboard_api as api


client = TestClient(api.app)


def _fake_conn():
    conn = MagicMock()
    conn.__enter__ = lambda s: conn
    conn.__exit__ = lambda s, *a: False
    return conn


# -- bulk playlist processing ----------------------------------------------

def test_bulk_process_playlist_queues_pending_and_skips_complete():
    rows = [
        {"track_id": 1, "artist": "A", "title": "One", "url": "https://youtube.com/watch?v=a"},
        {"track_id": 2, "artist": "B", "title": "Two", "url": "https://youtube.com/watch?v=b"},
    ]
    with patch("karaoke.localcache.connect", return_value=_fake_conn()), \
         patch("scripts.enqueue_playlists.playlist_tracks", return_value=rows), \
         patch("karaoke.postprocess_queue.needs_postprocessing",
               side_effect=[["sync"], []]), \
         patch("karaoke.postprocess_queue.publish_postprocess_task", return_value=True) as pub:
        res = client.post("/api/backstage/playlists/my-list/bulk-process")

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["queued"] == 1
    assert body["already_complete"] == 1
    assert body["total"] == 2
    pub.assert_called_once()


def test_bulk_process_playlist_respects_limit():
    rows = [
        {"track_id": i, "artist": "A", "title": str(i), "url": "https://youtube.com/watch?v=x"}
        for i in range(5)
    ]
    with patch("karaoke.localcache.connect", return_value=_fake_conn()), \
         patch("scripts.enqueue_playlists.playlist_tracks", return_value=rows), \
         patch("karaoke.postprocess_queue.needs_postprocessing", return_value=["sync"]), \
         patch("karaoke.postprocess_queue.publish_postprocess_task", return_value=True) as pub:
        res = client.post("/api/backstage/playlists/my-list/bulk-process?limit=2")

    body = res.json()
    assert body["queued"] == 2
    assert pub.call_count == 2


# -- gap-fill background job ------------------------------------------------

def test_gap_fill_start_launches_transient_unit():
    ok_result = MagicMock(returncode=0, stderr="")
    with patch("karaoke.dashboard_api.subprocess.run", return_value=ok_result) as run:
        res = client.post("/api/backstage/gap-fill/start")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    cmd = run.call_args.args[0]
    assert cmd[0] == "systemd-run"
    assert any("fill_analysis_and_vector_gaps.py" in c for c in cmd)


def test_gap_fill_start_reports_systemd_error():
    bad_result = MagicMock(returncode=1, stderr="Unit karaoke-backstage-gapfill.service already exists.")
    with patch("karaoke.dashboard_api.subprocess.run", return_value=bad_result):
        res = client.post("/api/backstage/gap-fill/start")
    body = res.json()
    assert body["status"] == "error"
    assert "already exists" in body["error"]


def test_gap_fill_status_reports_running_and_log_tail():
    active_result = MagicMock(stdout="active\n")
    journal_result = MagicMock(stdout="line one\nline two\n")
    with patch("karaoke.dashboard_api.subprocess.run", side_effect=[active_result, journal_result]):
        res = client.get("/api/backstage/gap-fill/status")
    body = res.json()
    assert body["running"] is True
    assert body["log_tail"] == ["line one", "line two"]


def test_gap_fill_stop_calls_systemctl_stop():
    ok_result = MagicMock(returncode=0, stderr="")
    with patch("karaoke.dashboard_api.subprocess.run", return_value=ok_result) as run:
        res = client.post("/api/backstage/gap-fill/stop")
    body = res.json()
    assert body["status"] == "ok"
    cmd = run.call_args.args[0]
    assert cmd == ["systemctl", "--user", "stop", "karaoke-backstage-gapfill"]
