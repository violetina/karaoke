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
