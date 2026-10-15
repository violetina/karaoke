"""Live listeners -> stage views: anchors, staleness, and recording marks."""
import os
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from karaoke import live_state, localcache, recorder
from karaoke.recording_slice import RECOGNITION_LEAD_S


@pytest.fixture()
def live_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(live_state, "state_dir", lambda: tmp_path / "live")
    return tmp_path / "live"


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_anchor_converts_monotonic_to_wall():
    assert live_state.anchor_wall(100.0, now_wall=5000.0, now_mono=110.0) == 4990.0


def test_position_reckons_forward_with_lead_and_nudge():
    state = {"offset": 30.0, "anchor_wall": 1000.0, "lead": 12.6, "nudge": 1.0}
    assert live_state.position(state, now=1010.0) == pytest.approx(53.6)


def test_position_is_unknown_before_a_match():
    assert live_state.position({"offset": None, "anchor_wall": None}) is None


def test_published_state_reads_back(live_dir):
    live_state.publish("radio", artist="Mr. Bungle", title="Sweet Charity",
                       offset=42.0, offset_mono=time.monotonic(), nudge=12.6)
    state = live_state.read("radio")
    assert state["title"] == "Sweet Charity"
    assert live_state.position(state) == pytest.approx(54.6, abs=0.5)


def test_a_stale_heartbeat_is_ignored(live_dir):
    live_state.publish("radio", artist="a", title="t", offset=1.0,
                       offset_mono=time.monotonic())
    later = time.time() + live_state.STALE_AFTER_S + 1
    assert live_state.read("radio", now=later) is None


def test_a_dead_publisher_is_ignored(live_dir):
    live_state.publish("radio", artist="a", title="t", offset=1.0,
                       offset_mono=time.monotonic())
    path = live_dir / "radio.json"
    path.write_text(path.read_text().replace(f'"pid": {os.getpid()}',
                                             f'"pid": {_dead_pid()}'))
    assert live_state.read("radio") is None


def test_current_skips_listeners_without_a_song(live_dir):
    live_state.publish("tui-radio", artist="", title="", offset=None, offset_mono=None)
    assert live_state.current() is None
    live_state.publish("radio", artist="a", title="t", offset=1.0,
                       offset_mono=time.monotonic())
    assert live_state.current()["source"] == "radio"


def test_clear_leaves_another_process_state_alone(live_dir):
    live_state.publish("radio", artist="a", title="t", offset=1.0,
                       offset_mono=time.monotonic())
    path = live_dir / "radio.json"
    path.write_text(path.read_text().replace(f'"pid": {os.getpid()}',
                                             f'"pid": {os.getppid()}'))
    live_state.clear("radio")
    assert path.exists()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    real = localcache.connect
    monkeypatch.setattr(localcache, "connect", lambda *a, **k: real(path))
    c = localcache.connect(path)
    localcache.ensure_recording_tables(c)
    recorder._ensure_owner_column(c)
    c.commit()
    c.close()
    return path


def _recording(status="recording", pid=None):
    with localcache.connect() as c:
        rid = c.execute(
            "INSERT INTO recordings (started_at, source, dir, status, pid)"
            " VALUES (%s, 'mon', '/tmp', %s, %s) RETURNING recording_id",
            (time.time(), status, pid)).fetchone()["recording_id"]
        c.commit()
        return int(rid)


def _mark(rid, at_wall, at_offset, title="Song"):
    with localcache.connect() as c:
        c.execute("INSERT INTO recording_marks (recording_id, at_wall, at_offset, artist, title, ok)"
                  " VALUES (%s, %s, %s, 'Artist', %s, 1)", (rid, at_wall, at_offset, title))
        c.commit()


def test_recording_playhead_corrects_for_recognition_lag(db):
    now = 10_000.0
    rid = _recording()
    _mark(rid, now - 10.0, 50.0)
    with localcache.connect() as c:
        head = live_state.recording_playhead(c, now=now)
    assert head["title"] == "Song"
    assert head["position_s"] == pytest.approx(50.0 + 10.0 + RECOGNITION_LEAD_S)


def test_an_old_mark_says_nothing(db):
    now = 10_000.0
    _mark(_recording(), now - 600.0, 50.0)
    with localcache.connect() as c:
        assert live_state.recording_playhead(c, now=now) is None


def test_a_finished_recording_says_nothing(db):
    now = 10_000.0
    _mark(_recording(status="complete"), now - 5.0, 50.0)
    with localcache.connect() as c:
        assert live_state.recording_playhead(c, now=now) is None


def test_reconcile_spares_a_recording_another_live_process_owns(db):
    alive = _recording(pid=os.getppid())     # e.g. `karaoke -r --record`
    dead = _recording(pid=_dead_pid())
    unknown = _recording(pid=None)            # rows from before the column
    closed = recorder.reconcile_stale()
    assert alive not in closed
    assert set(closed) >= {dead, unknown}


def test_stage_follows_radio_when_no_player_plays():
    from karaoke.stage_view import get_stage_state

    live = {"source": "radio", "artist": "Mr. Bungle", "title": "Sweet Charity",
            "position_s": 61.5}
    with patch("karaoke.player_open.browser_playback", return_value=None), \
         patch("karaoke.playerctl.playing_player", return_value=None), \
         patch("karaoke.localcache.load_active_queue", return_value=None), \
         patch("karaoke.stage_view._live_listener", return_value=live):
        state = get_stage_state()
    assert state["status"] == "Playing"
    assert state["sync_source"] == "radio"
    assert state["title"] == "Sweet Charity"
    assert state["position_s"] == 61.5


def test_a_playing_desktop_player_wins_over_radio():
    from types import SimpleNamespace

    from karaoke.stage_view import get_stage_state

    meta = SimpleNamespace(artist="Player Artist", title="Player Song", album="",
                           url="", duration=200.0)
    live = {"source": "radio", "artist": "x", "title": "y", "position_s": 1.0}
    with patch("karaoke.player_open.browser_playback", return_value=None), \
         patch("karaoke.playerctl.playing_player", return_value="chromium"), \
         patch("karaoke.playerctl.current_metadata", return_value=meta), \
         patch("karaoke.playerctl.status", return_value="Playing"), \
         patch("karaoke.playerctl.position", return_value=10.0), \
         patch("karaoke.playerctl.art_url", return_value=""), \
         patch("karaoke.localcache.load_active_queue", return_value=None), \
         patch("karaoke.stage_view._live_listener", return_value=live) as listener:
        state = get_stage_state()
    assert state["title"] == "Player Song"
    assert state["sync_source"] == "player"
    listener.assert_not_called()
