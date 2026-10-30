"""Dashboard right-click song actions (similarity graph context menu)."""
from fastapi.testclient import TestClient

from karaoke import dashboard_api as api


client = TestClient(api.app)


class _FakeResp:
    status_code = 200

    def json(self):
        # The ctrl-api queue endpoint reports its own status == "ok".
        return {"status": "ok", "video_id": "abc123", "position": "next"}


class _FakeClient:
    """Minimal async httpx.AsyncClient stand-in for the ctrl-api POST."""

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _FakeResp()


def test_queue_song_reports_queued_even_when_ctrl_api_says_ok(monkeypatch):
    """Regression: a successful queue must return status == "queued".

    The ctrl-api payload carries status == "ok". Spreading it over our
    {"status": "queued"} let "ok" win, so the frontend's queued-check failed
    and the user saw "Could not queue this song" on a track that WAS queued.
    """
    monkeypatch.setattr(api.httpx, "AsyncClient", _FakeClient)

    # Passing video_id skips source resolution and goes straight to the POST.
    resp = client.post("/api/song/queue", json={"video_id": "abc123"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "queued"
    assert data["video_id"] == "abc123"
