"""Backstage Line Looper backend: lines listing + save endpoints."""
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from karaoke import dashboard_api as api


client = TestClient(api.app)


def _fake_conn():
    conn = MagicMock()
    conn.__enter__ = lambda s: conn
    conn.__exit__ = lambda s, *a: False
    return conn


def test_line_looper_track_returns_parsed_lines():
    from karaoke.lyrics import Lyrics

    synced = "[00:10.00] I been wondering\n[00:13.00] where you are"
    lyr = Lyrics(plain="", synced_raw=synced, source="lrclib",
                 lines=[(10.0, "I been wondering"), (13.0, "where you are")])

    with patch("karaoke.localcache.connect", return_value=_fake_conn()), \
         patch("karaoke.localcache.get_lyrics_by_track_id", return_value=lyr), \
         patch("karaoke.track_analysis.get_analysis", return_value=MagicMock(bpm=120.0)), \
         patch("karaoke.dashboard_api._best_source_url_for_track", return_value="https://www.youtube.com/watch?v=x"), \
         patch("karaoke.postprocess_worker._get_cached_audio_path", return_value="/tmp/x.webm"):
        res = client.get("/api/backstage/line-looper/track/42?media_type=audio")

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["bpm"] == 120.0
    assert body["has_local_file"] is True
    assert len(body["lines"]) == 2
    assert body["lines"][0]["words"] == ["I", "been", "wondering"]
    assert body["lines"][0]["start"] == 10.0


def test_line_looper_track_no_lyrics_returns_error():
    with patch("karaoke.localcache.connect", return_value=_fake_conn()), \
         patch("karaoke.localcache.get_lyrics_by_track_id", return_value=None):
        res = client.get("/api/backstage/line-looper/track/42")
    body = res.json()
    assert body["status"] == "error"
    assert body["lines"] == []


def test_line_looper_save_distributes_and_persists():
    from karaoke.lyrics import Lyrics

    synced = "[00:10.00] I been wondering"
    lyr = Lyrics(plain="", synced_raw=synced, source="lrclib",
                 lines=[(10.0, "I been wondering")])

    with patch("karaoke.localcache.connect", return_value=_fake_conn()), \
         patch("karaoke.localcache.get_lyrics_by_track_id", return_value=lyr), \
         patch("karaoke.track_analysis.get_analysis", return_value=MagicMock(bpm=None)), \
         patch("karaoke.dashboard_api._resolve_artist_title_url",
               return_value=("Dolly Parton", "Jolene", "")), \
         patch("karaoke.localcache.put_cached_lyrics") as put_mock:
        res = client.post("/api/backstage/line-looper/save", json={
            "track_id": 42, "media_type": "audio",
            "old_start": 10.0, "new_start": 10.2, "new_end": 11.0,
            "words": ["I", "been", "wondering"],
        })

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert len(body["word_times"]) == 3
    assert body["word_times"][0] == 10.2
    assert body["word_times"][-1] == 11.0
    assert "<00:10.20>I" in body["line"]
    put_mock.assert_called_once()
    args, kwargs = put_mock.call_args
    assert args[0] == "Dolly Parton" and args[1] == "Jolene"
    assert kwargs["media_type"] == "audio"


def test_line_looper_save_dry_run_does_not_persist():
    from karaoke.lyrics import Lyrics

    lyr = Lyrics(plain="", synced_raw="[00:10.00] I been wondering", source="lrclib",
                 lines=[(10.0, "I been wondering")])
    with patch("karaoke.localcache.connect", return_value=_fake_conn()), \
         patch("karaoke.localcache.get_lyrics_by_track_id", return_value=lyr), \
         patch("karaoke.track_analysis.get_analysis", return_value=None), \
         patch("karaoke.localcache.put_cached_lyrics") as put_mock:
        res = client.post("/api/backstage/line-looper/save", json={
            "track_id": 42, "old_start": 10.0, "new_start": 10.0, "new_end": 10.8,
            "words": ["I", "been", "wondering"], "dry_run": True,
        })
    body = res.json()
    assert body["status"] == "ok"
    assert "Preview" in body["message"]
    put_mock.assert_not_called()


def test_line_looper_save_rejects_end_before_start():
    from karaoke.lyrics import Lyrics

    lyr = Lyrics(plain="", synced_raw="[00:10.00] hi", source="lrclib", lines=[(10.0, "hi")])
    with patch("karaoke.localcache.connect", return_value=_fake_conn()), \
         patch("karaoke.localcache.get_lyrics_by_track_id", return_value=lyr), \
         patch("karaoke.track_analysis.get_analysis", return_value=None):
        res = client.post("/api/backstage/line-looper/save", json={
            "track_id": 42, "old_start": 10.0, "new_start": 10.0, "new_end": 9.0,
            "words": ["hi"],
        })
    body = res.json()
    assert body["status"] == "error"
