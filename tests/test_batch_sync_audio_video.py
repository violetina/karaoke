"""scripts/batch_sync_audio_video.py: logging via the shared helper, --force-waveform."""
from karaoke import localcache
from scripts import batch_sync_audio_video as batch


def test_process_one_track_passes_force_waveform_and_logs(tmp_path, monkeypatch):
    conn = localcache.connect(tmp_path / "test.db")
    tid = conn.execute("INSERT INTO tracks (artist, title) VALUES ('A', 'T') "
                       "RETURNING track_id").fetchone()["track_id"]
    conn.commit()
    seen = {}

    def fake_sync(track_id, c, **kw):
        seen.update(kw)
        return {"offset": -4.04, "method": "waveform", "confidence": 0.8,
                "has_audio_lyrics": True, "has_video_lyrics": True}

    monkeypatch.setattr(batch, "sync_audio_video_counterparts", fake_sync)
    monkeypatch.setattr(batch, "_get_thread_conn", lambda: conn)
    monkeypatch.setattr(batch, "_get_thread_client", lambda: object())

    res = batch.process_one_track({"track_id": tid, "artist": "A", "title": "T", "url": None},
                                  force_waveform=True)
    assert seen["force_waveform"] is True
    assert res["status"] == "synced"
    row = conn.execute("SELECT method FROM av_sync_log WHERE track_id = %s", (tid,)).fetchone()
    assert row["method"] == "waveform"
    conn.close()


def test_celery_sync_av_records_av_sync_log(tmp_path, monkeypatch):
    from karaoke import tasks

    conn = localcache.connect(tmp_path / "test.db")
    tid = conn.execute("INSERT INTO tracks (artist, title) VALUES ('B', 'U') "
                       "RETURNING track_id").fetchone()["track_id"]
    conn.commit()
    # deferred imports inside the task resolve from the source modules
    monkeypatch.setattr("karaoke.caption_sync.sync_audio_video_counterparts",
                        lambda track_id, c, **kw: {"offset": 1.5, "method": "waveform",
                                                   "confidence": 0.7, "has_audio_lyrics": True,
                                                   "has_video_lyrics": True})
    monkeypatch.setattr("karaoke.ytmusic_client.YTMusicClient", lambda *a, **k: object())
    monkeypatch.setattr("karaoke.localcache.connect", lambda *a, **k: conn)

    tasks.sync_av({"track_id": tid})
    row = conn.execute("SELECT status, method FROM av_sync_log WHERE track_id = %s", (tid,)).fetchone()
    assert (row["status"], row["method"]) == ("synced", "waveform")


def _track_with_two_youtube_sources(conn, artist="Queen", title="Song"):
    tid = conn.execute("INSERT INTO tracks (artist, title) VALUES (%s, %s) RETURNING track_id",
                       (artist, title)).fetchone()["track_id"]
    conn.execute("INSERT INTO lyrics (track_id, kind, synced_lyrics) VALUES (%s, 'approved', %s)",
                 (tid, "[00:01.00] hello"))
    conn.execute("INSERT INTO sources (track_id, kind, url) VALUES (%s, 'youtube_music', %s)",
                 (tid, f"https://music.youtube.com/watch?v=audio{tid}"))
    conn.execute("INSERT INTO sources (track_id, kind, url) VALUES (%s, 'youtube_video', %s)",
                 (tid, f"https://www.youtube.com/watch?v=video{tid}"))
    conn.commit()
    return tid


def test_find_candidates_returns_each_track_once_for_track_ids(tmp_path):
    conn = localcache.connect(tmp_path / "test.db")
    batch.ensure_log_table(conn)
    tid = _track_with_two_youtube_sources(conn)
    rows = batch.find_candidates(conn, track_ids=[tid])
    assert [r["track_id"] for r in rows] == [tid]
    assert rows[0]["url"] == f"https://www.youtube.com/watch?v=video{tid}"
    conn.close()


def test_find_candidates_default_query_returns_each_track_once(tmp_path):
    conn = localcache.connect(tmp_path / "test.db")
    batch.ensure_log_table(conn)
    tid = _track_with_two_youtube_sources(conn)
    rows = batch.find_candidates(conn, limit=1000)
    assert [r["track_id"] for r in rows].count(tid) == 1
    conn.close()
