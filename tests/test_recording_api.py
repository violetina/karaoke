"""API surface for record mode.

The split matters: inspecting recordings is read-only over SQLite and lives in
karaoke.api, which is cluster-deployable. Starting a capture needs PipeWire, a
desktop audio session and a long-lived ffmpeg child, so it lives in the
host-side control API instead.
"""
import pytest
from fastapi.testclient import TestClient

from karaoke import localcache


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    real = localcache.connect
    monkeypatch.setattr(localcache, "connect", lambda *a, **k: real(path))
    c = real(path)
    c.executescript("""
        INSERT INTO recordings (recording_id, started_at, ended_at, source, dir,
                                status, keep_audio)
        VALUES (1, 1000.0, 2000.0, 'x.monitor', '/tmp/nope', 'complete', 0);
        INSERT INTO recording_marks (recording_id, at_wall, at_offset, artist,
                                     title, ok) VALUES
            (1, 1000.0, 30.0, 'Portishead', 'Glory Box', 1),
            (1, 1045.0, 75.0, 'Portishead', 'Glory Box', 1),
            (1, 1090.0, NULL, '', '', 0);
    """)
    c.commit()
    c.close()
    return path


# -- read-only API ------------------------------------------------------

@pytest.fixture()
def api(db):
    from karaoke.api import app
    return TestClient(app)


def test_listing_reports_marks_and_identification(api):
    body = api.get("/api/recordings").json()
    assert body["count"] == 1
    row = body["recordings"][0]
    assert row["marks"] == 3 and row["identified"] == 2
    assert row["status"] == "complete"


def test_listing_separates_stored_status_from_a_live_capture(api):
    """A row can read 'recording' after a crash, so the two must not be derived
    from each other."""
    row = api.get("/api/recordings").json()["recordings"][0]
    assert row["running"] is False


def test_detail_derives_the_track_list(api):
    body = api.get("/api/recordings/1").json()
    assert body["identified"] == 2
    assert len(body["tracks"]) == 1
    track = body["tracks"][0]
    assert track["title"] == "Glory Box"
    assert track["marks"] == 2
    assert track["spread_s"] == pytest.approx(0.0)
    assert track["confident"] is True


def test_detail_reports_a_missing_recording(api):
    assert api.get("/api/recordings/999").status_code == 404


def test_detail_flags_tracks_with_no_captured_audio(api):
    """The audio directory does not exist here, so nothing can be analysed."""
    track = api.get("/api/recordings/1").json()["tracks"][0]
    assert track["audio_available"] is False


# -- control API --------------------------------------------------------

@pytest.fixture()
def ctrl(db):
    from karaoke.ctrl_api import app
    return TestClient(app)


def test_status_is_empty_with_nothing_running(ctrl):
    assert ctrl.get("/api/record/status").json() == {"recording": [], "count": 0}


def test_start_reports_a_conflict_when_nothing_is_playing(ctrl, monkeypatch):
    """Not a server error: there is simply no output to record."""
    from karaoke import recorder
    monkeypatch.setattr(recorder, "start", lambda *a, **k: (_ for _ in ()).throw(
        recorder.RecorderError("nothing is playing; no output to record")))
    resp = ctrl.post("/api/record/start", json={})
    assert resp.status_code == 409
    assert "nothing is playing" in resp.json()["detail"]


def test_stopping_an_unknown_recording_is_a_404(ctrl):
    assert ctrl.post("/api/record/stop", json={"recording_id": 99}).status_code == 404


def test_analysing_an_unknown_recording_is_a_404(ctrl):
    assert ctrl.post("/api/recordings/99/analyse").status_code == 404


def test_analysing_a_running_capture_is_refused(ctrl, monkeypatch):
    """Its audio is still being written; the track list would be incomplete."""
    from karaoke import recorder
    monkeypatch.setattr(recorder, "is_running", lambda rid: True)
    assert ctrl.post("/api/recordings/1/analyse").status_code == 409


def test_analyse_returns_before_it_finishes(ctrl, monkeypatch):
    """Hours of audio take minutes; no HTTP client should hold that open."""
    from karaoke import recorder
    monkeypatch.setattr(recorder, "is_running", lambda rid: False)
    resp = ctrl.post("/api/recordings/1/analyse")
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


def test_discarding_a_running_capture_is_refused(ctrl, monkeypatch):
    from karaoke import recorder
    monkeypatch.setattr(recorder, "is_running", lambda rid: True)
    assert ctrl.delete("/api/recordings/1/audio").status_code == 409


def test_sample_reports_an_unavailable_analysis_as_503(ctrl, monkeypatch):
    """The capture worked; this host just cannot examine it."""
    from karaoke import sample_audio
    monkeypatch.setattr(sample_audio, "sample_and_analyse",
                        lambda *a, **k: (_ for _ in ()).throw(
                            sample_audio.AnalysisUnavailable("no audio stack")))
    assert ctrl.post("/api/sample", json={}).status_code == 503


def test_sample_reports_a_capture_failure_as_409(ctrl, monkeypatch):
    from karaoke import sample_audio
    monkeypatch.setattr(sample_audio, "sample_and_analyse",
                        lambda *a, **k: (_ for _ in ()).throw(
                            sample_audio.CaptureError("nothing is playing")))
    assert ctrl.post("/api/sample", json={}).status_code == 409


def test_sample_returns_the_analysis(ctrl, monkeypatch):
    from karaoke import sample_audio
    from karaoke.musictheory import parse_key

    class Result:
        key = parse_key("D minor")
        bpm = 129.2

    monkeypatch.setattr(sample_audio, "sample_and_analyse", lambda *a, **k: Result())
    body = ctrl.post("/api/sample", json={"artist": "A", "title": "B"}).json()
    assert body["key"] == "D minor" and body["bpm"] == 129.2
    assert body["stored"] is True


def test_sample_without_a_track_is_not_stored(ctrl, monkeypatch):
    from karaoke import sample_audio

    class Result:
        key = None
        bpm = 100.0

    monkeypatch.setattr(sample_audio, "sample_and_analyse", lambda *a, **k: Result())
    assert ctrl.post("/api/sample", json={}).json()["stored"] is False
