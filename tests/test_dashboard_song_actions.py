"""Dashboard right-click song actions (similarity graph context menu)."""
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from karaoke import dashboard_api as api


client = TestClient(api.app)


def test_queue_song_adds_to_local_queue_by_track_id():
    """Right-click Queue next adds the track to the local karaoke queue."""
    with patch("karaoke.localcache.connect") as connect, \
         patch("karaoke.collection.enqueue_next",
               return_value={"status": "queued", "position": 3,
                             "artist": "Queen", "title": "Flash"}) as enq:
        connect.return_value.__enter__.return_value = MagicMock()
        res = client.post("/api/song/queue", json={"track_id": 42})

    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "queued"
    assert data["title"] == "Flash"
    # The resolved track_id is forwarded to the local-queue inserter.
    assert enq.call_args.args[0] == 42


def test_queue_song_resolves_track_id_from_artist_title():
    with patch("karaoke.localcache.connect") as connect, \
         patch("karaoke.localcache.find_track_id", return_value=77) as find, \
         patch("karaoke.collection.enqueue_next",
               return_value={"status": "queued", "position": 1}) as enq:
        connect.return_value.__enter__.return_value = MagicMock()
        res = client.post("/api/song/queue", json={"artist": "Queen", "title": "Flash"})

    assert res.status_code == 200
    assert res.json()["status"] == "queued"
    find.assert_called_once()
    assert enq.call_args.args[0] == 77


def test_queue_song_reports_when_not_in_library():
    with patch("karaoke.localcache.connect") as connect, \
         patch("karaoke.localcache.find_track_id", return_value=None):
        connect.return_value.__enter__.return_value = MagicMock()
        res = client.post("/api/song/queue", json={"artist": "Nobody", "title": "Nothing"})

    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "error"
    assert "message" in data
