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
