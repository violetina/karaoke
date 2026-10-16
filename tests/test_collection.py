"""The graph collector: rows, save as playlist, play as queue."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from karaoke import collection


def _conn(rows):
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = rows
    return conn


def _row(tid, url, kind="youtube"):
    return {"track_id": tid, "artist": f"A{tid}", "title": f"T{tid}", "bpm": 100.0,
            "detected_key": "A minor", "energy": 0.5, "url": url, "kind": kind}


ROWS = [_row(1, "https://music.youtube.com/watch?v=aaaaaaaaaaa", "youtube_music"),
        _row(2, ""),                                       # no YouTube source
        _row(3, "https://www.youtube.com/watch?v=ccccccccccc")]


def test_rows_keep_the_collector_order_and_drop_unknown_ids():
    rows = collection.rows_for([3, 99, 1, 2], _conn(ROWS))
    assert [r["track_id"] for r in rows] == [3, 1, 2]


def test_save_writes_a_graph_playlist(monkeypatch):
    saved = {}
    monkeypatch.setattr("karaoke.localcache.save_playlist",
                        lambda pid, name, **kw: saved.update(pid=pid, name=name, **kw))
    out = collection.save("Late Night Swings!", [1, 3], _conn(ROWS))
    assert out == {"playlist_id": "graph-late-night-swings", "name": "Late Night Swings!", "count": 2}
    assert saved["source_kind"] == "graph"
    assert [t["video_id"] for t in saved["tracks"]] == ["aaaaaaaaaaa", "ccccccccccc"]


@pytest.fixture()
def playback(monkeypatch):
    calls = {"queue": None, "opened": []}
    monkeypatch.setattr("karaoke.localcache.save_active_queue",
                        lambda pid, name, rows, **kw: calls.update(queue=(pid, [r["track_id"] for r in rows])))
    monkeypatch.setattr("karaoke.localcache.record_queue_event", lambda *a, **k: 1)
    monkeypatch.setattr("karaoke.browse.open_song_url", lambda url, kind, **kw: calls["opened"].append(url))
    return calls


def test_play_opens_a_youtube_music_queue_and_skips_songs_without_a_source(monkeypatch, playback):
    monkeypatch.setattr("karaoke.ytmusic_playlist.create_temp_queue_playlist",
                        lambda rows, **kw: SimpleNamespace(playlist_id="PLx"))
    out = collection.play("", [1, 2, 3], _conn(ROWS))
    assert out["status"] == "playing" and out["mode"] == "ytmusic_queue" and out["count"] == 2
    assert out["skipped"] == ["A2 - T2"]
    assert playback["queue"] == (collection.COLLECTION_QUEUE_ID, [1, 3])
    assert playback["opened"] == ["https://music.youtube.com/watch?v=aaaaaaaaaaa&list=PLx"]


def test_without_youtube_music_the_first_song_still_plays(monkeypatch, playback):
    def boom(rows, **kw):
        raise RuntimeError("not signed in")
    monkeypatch.setattr("karaoke.ytmusic_playlist.create_temp_queue_playlist", boom)
    out = collection.play("", [1, 3], _conn(ROWS))
    assert out["mode"] == "first_song_only"
    assert playback["opened"] == ["https://music.youtube.com/watch?v=aaaaaaaaaaa"]
    assert playback["queue"][1] == [1, 3]           # the TUI can still follow the rest


def test_nothing_playable_plays_nothing(playback):
    out = collection.play("", [2], _conn(ROWS))
    assert out["status"] == "nothing_playable" and playback["opened"] == []


def test_endpoints_refuse_an_empty_collection():
    from starlette.testclient import TestClient

    from karaoke.mcp_server import create_asgi_app

    client = TestClient(create_asgi_app())
    assert client.post("/api/collection/save", json={"track_ids": []}).status_code == 400
    assert client.post("/api/collection/play", json={"track_ids": []}).status_code == 400


def test_the_page_has_the_collector():
    from karaoke.graph_view import GRAPH_HTML

    assert 'id="collector-drop"' in GRAPH_HTML and "function playCollection" in GRAPH_HTML
