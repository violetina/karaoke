"""Tests for the stage / TV prompter view and SSE streaming endpoint."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from karaoke.stage_view import get_stage_state, render_stage_html, stage_event_stream


@pytest.fixture()
def client():
    from karaoke.ctrl_api import app
    return TestClient(app)


def test_render_stage_html():
    html_text = render_stage_html()
    assert "<!DOCTYPE html>" in html_text
    assert "Karaoke Stage View" in html_text
    assert 'id="rhythm-bar"' in html_text
    assert 'id="lyrics-container"' in html_text
    assert 'id="line-active"' in html_text
    assert 'id="line-next"' in html_text
    assert 'id="up-next-track"' in html_text
    assert 'id="cast-badge"' in html_text
    assert "/api/stage/stream" in html_text
    assert "EventSource" in html_text


def test_get_stage_state_idle():
    with patch("karaoke.player_open.browser_playback", return_value=None), \
         patch("karaoke.playerctl.playing_player", return_value=None), \
         patch("karaoke.localcache.load_active_queue", return_value=None), \
         patch("karaoke.stage_view._live_listener", return_value=None):
        state = get_stage_state()
        assert state["artist"] == ""
        assert state["title"] == ""
        assert state["status"] == "Stopped"
        assert state["casting"] is False
        assert state["lines"] == []
        assert state["upcoming_queue"] == []
        assert state["active_line_index"] == -1


def test_get_stage_state_with_playback_and_lyrics():
    fake_playback = {
        "present": True,
        "position": 15.5,
        "duration": 200.0,
        "casting": True,
        "paused": False,
        "url": "https://music.youtube.com/watch?v=xyz",
    }
    fake_meta = MagicMock(artist="Queen", title="Bohemian Rhapsody", album="A Night at the Opera", url=None, duration=None)

    raw_lrc = (
        "[00:10.00]Is this the real life?\n"
        "[00:15.00]Is this just fantasy?\n"
        "[00:20.00]Caught in a landslide\n"
    )

    fake_ly = MagicMock()
    fake_ly.synced_raw = raw_lrc
    fake_ly.lines = [(10.0, "Is this the real life?"), (15.0, "Is this just fantasy?"), (20.0, "Caught in a landslide")]

    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = {
        "bpm": 72.0,
        "detected_key": "Bb Major",
        "energy": 0.65,
    }

    fake_queue = {
        "current_index": 0,
        "rows": [
            {"artist": "Queen", "title": "Bohemian Rhapsody"},
            {"artist": "Queen", "title": "Don't Stop Me Now"},
        ],
    }

    with patch("karaoke.player_open.browser_playback", return_value=fake_playback), \
         patch("karaoke.playerctl.playing_player", return_value="chromium"), \
         patch("karaoke.playerctl.current_metadata", return_value=fake_meta), \
         patch("karaoke.playerctl.status", return_value="Playing"), \
         patch("karaoke.playerctl.art_url", return_value="https://art.example/cover.jpg"), \
         patch("karaoke.localcache.connect") as mock_conn, \
         patch("karaoke.localcache.find_track_id", return_value=123), \
         patch("karaoke.localcache.get_lyrics_by_track_id", return_value=fake_ly), \
         patch("karaoke.track_analysis.ensure_schema"), \
         patch("karaoke.localcache.load_active_queue", return_value=fake_queue):

        mock_conn.return_value.__enter__.return_value = fake_conn

        state = get_stage_state()
        assert state["artist"] == "Queen"
        assert state["title"] == "Bohemian Rhapsody"
        assert state["album"] == "A Night at the Opera"
        assert state["position_s"] == 15.5
        assert state["duration"] == 200.0
        assert state["casting"] is True
        assert state["status"] == "Playing"
        assert state["bpm"] == 72.0
        assert state["key"] == "Bb Major"
        assert len(state["lines"]) >= 3
        # At 15.5s, line 1 ([00:15.00] "Is this just fantasy?") is active
        assert state["active_line_index"] == 1
        assert len(state["upcoming_queue"]) == 1
        assert state["upcoming_queue"][0]["title"] == "Don't Stop Me Now"


def test_get_stage_state_passes_current_av_mode_to_lyrics_lookup():
    """Regression: get_stage_state used to call get_lyrics_by_track_id with no
    media_type at all, so it just grabbed whichever lyrics row had the
    highest lyric_id -- almost always the newer AV-sync-derived row once a
    track has both an audio and a video lyrics row. Found live: PJ Harvey
    "Man-Size" showed lyrics ~3.7s late in BOTH audio and video playback
    mode, because the stage always served the video-shifted copy regardless
    of which mode was actually playing. Fixed by asking the kiosk's real AV
    toggle state (player_open.cdp_get_av) and passing it through."""
    fake_playback = {
        "present": True, "position": 15.5, "duration": 200.0,
        "casting": True, "paused": False,
        "url": "https://music.youtube.com/watch?v=xyz",
    }
    fake_meta = MagicMock(artist="PJ Harvey", title="Man-Size", album="", url=None, duration=None)
    fake_ly = MagicMock()
    fake_ly.synced_raw = "[00:10.00]Line one\n"
    fake_ly.lines = [(10.0, "Line one")]

    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = {
        "bpm": None, "detected_key": None, "energy": None,
    }

    with patch("karaoke.player_open.browser_playback", return_value=fake_playback), \
         patch("karaoke.player_open.cdp_get_av", return_value="video") as mock_av, \
         patch("karaoke.playerctl.playing_player", return_value="chromium"), \
         patch("karaoke.playerctl.current_metadata", return_value=fake_meta), \
         patch("karaoke.playerctl.status", return_value="Playing"), \
         patch("karaoke.playerctl.art_url", return_value=""), \
         patch("karaoke.localcache.connect") as mock_conn, \
         patch("karaoke.localcache.find_track_id", return_value=923), \
         patch("karaoke.localcache.get_lyrics_by_track_id", return_value=fake_ly) as mock_get_lyrics, \
         patch("karaoke.track_analysis.ensure_schema"), \
         patch("karaoke.localcache.load_active_queue", return_value={"current_index": 0, "rows": []}):

        mock_conn.return_value.__enter__.return_value = fake_conn

        get_stage_state()

        mock_av.assert_called_once()
        mock_get_lyrics.assert_called_once_with(923, fake_conn, media_type="video")


def test_stage_state_flags_instrumental_break_like_the_tui():
    """A long no-lyric stretch must expose in_gap/gap_progress for the prompter.

    Mirrors the TUI's LyricTimeline.in_gap rest marker: between the end of a
    line and a next line that is >= GAP_MIN_S away, the stage reports the break
    so the dashboard can show a musical-note countdown instead of a frozen line.
    """
    fake_playback = {
        "present": True,
        "position": 25.0,  # inside the 10s -> 40s instrumental break
        "duration": 200.0,
        "casting": False,
        "paused": False,
        "url": "https://music.youtube.com/watch?v=xyz",
    }
    fake_meta = MagicMock(artist="Pink Floyd", title="Any Colour You Like",
                          album="Dark Side", url=None, duration=None)

    raw_lrc = (
        "[00:10.00]Opening line before the solo\n"
        "[00:40.00]Back to the words\n"
        "[00:45.00]And one more line\n"
    )
    fake_ly = MagicMock()
    fake_ly.synced_raw = raw_lrc
    fake_ly.lines = [(10.0, "Opening line before the solo"),
                     (40.0, "Back to the words"), (45.0, "And one more line")]

    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = {
        "bpm": 120.0, "detected_key": "D Major", "energy": 0.5,
    }

    with patch("karaoke.player_open.browser_playback", return_value=fake_playback), \
         patch("karaoke.playerctl.playing_player", return_value="chromium"), \
         patch("karaoke.playerctl.current_metadata", return_value=fake_meta), \
         patch("karaoke.playerctl.status", return_value="Playing"), \
         patch("karaoke.playerctl.art_url", return_value=""), \
         patch("karaoke.localcache.connect") as mock_conn, \
         patch("karaoke.localcache.find_track_id", return_value=321), \
         patch("karaoke.localcache.get_lyrics_by_track_id", return_value=fake_ly), \
         patch("karaoke.track_analysis.ensure_schema"), \
         patch("karaoke.localcache.load_active_queue", return_value=None):

        mock_conn.return_value.__enter__.return_value = fake_conn

        state = get_stage_state()
        assert state["in_gap"] is True
        assert 0.0 < state["gap_progress"] <= 1.0

        # Once the next line starts, the break is over.
        fake_playback["position"] = 41.0
        state2 = get_stage_state()
        assert state2["in_gap"] is False
        assert state2["gap_progress"] == 0.0


def _run_stage_with_queue(playing_artist, playing_title, playing_tid, queue):
    """Call get_stage_state with a mocked player + active queue."""
    fake_playback = {
        "present": True, "position": 2.0, "duration": 180.0,
        "casting": False, "paused": False, "url": "",
    }
    fake_meta = MagicMock(artist=playing_artist, title=playing_title,
                          album="", url=None, duration=None)
    fake_ly = MagicMock()
    fake_ly.synced_raw = ""
    fake_ly.lines = []
    fake_conn = MagicMock()
    fake_conn.execute.return_value.fetchone.return_value = None

    with patch("karaoke.player_open.browser_playback", return_value=fake_playback), \
         patch("karaoke.playerctl.playing_player", return_value="chromium"), \
         patch("karaoke.playerctl.current_metadata", return_value=fake_meta), \
         patch("karaoke.playerctl.status", return_value="Playing"), \
         patch("karaoke.playerctl.art_url", return_value=""), \
         patch("karaoke.localcache.connect") as mock_conn, \
         patch("karaoke.localcache.find_track_id", return_value=playing_tid), \
         patch("karaoke.localcache.get_lyrics_by_track_id", return_value=fake_ly), \
         patch("karaoke.track_analysis.ensure_schema"), \
         patch("karaoke.localcache.load_active_queue", return_value=queue):
        mock_conn.return_value.__enter__.return_value = fake_conn
        return get_stage_state()


def test_up_next_anchors_on_the_playing_track_not_stored_index():
    """UP NEXT must follow the song actually playing, not a stale current_index.

    The playing track sits at queue row 1 while current_index is still 0, so
    UP NEXT should list rows AFTER row 1, not after row 0.
    """
    queue = {
        "current_index": 0,
        "rows": [
            {"track_id": 10, "artist": "Queen", "title": "Bohemian Rhapsody"},
            {"track_id": 123, "artist": "Queen", "title": "Don't Stop Me Now"},
            {"track_id": 11, "artist": "Queen", "title": "We Will Rock You"},
            {"track_id": 12, "artist": "Queen", "title": "Somebody To Love"},
        ],
    }
    state = _run_stage_with_queue("Queen", "Don't Stop Me Now", 123, queue)
    titles = [q["title"] for q in state["upcoming_queue"]]
    assert titles == ["We Will Rock You", "Somebody To Love"]


def test_up_next_includes_current_index_row_when_playing_song_is_off_queue():
    """If the playing song is not in the queue, no row has been consumed yet,
    so UP NEXT starts at current_index (inclusive), not current_index + 1."""
    queue = {
        "current_index": 0,
        "rows": [
            {"track_id": 1, "artist": "Ren", "title": "The Tale of Jenny"},
            {"track_id": 2, "artist": "Inpatient", "title": "Madhouse"},
            {"track_id": 3, "artist": "Biohazard", "title": "Judgment Night"},
        ],
    }
    state = _run_stage_with_queue("L7", "Fast and Frightening", 999, queue)
    titles = [q["title"] for q in state["upcoming_queue"]]
    assert titles == ["The Tale of Jenny", "Madhouse", "Judgment Night"]


def test_stage_event_stream_generator():
    async def go():
        gen = stage_event_stream(interval=0.001)
        chunk = await anext(gen)
        assert chunk.startswith("data: ")
        assert chunk.endswith("\n\n")
        data = json.loads(chunk[6:-2])
        assert "timestamp" in data
        assert "lines" in data

    asyncio.run(go())


def test_ctrl_api_stage_endpoints(client):
    r_stage = client.get("/stage")
    assert r_stage.status_code == 200
    assert "text/html" in r_stage.headers["content-type"]
    assert "Karaoke" in r_stage.text

    r_tv = client.get("/tv")
    assert r_tv.status_code == 200
    assert "text/html" in r_tv.headers["content-type"]
    assert "Karaoke" in r_tv.text

    r_state = client.get("/api/stage/state")
    assert r_state.status_code == 200
    data = r_state.json()
    assert "lines" in data
    assert "casting" in data
    assert "status" in data


def test_a_line_without_mood_words_falls_back_to_the_song_not_the_room():
    from karaoke import stage_view

    stage_view._track_mood_cache.clear()
    lines = [{"text": "tears fall in the cold dark rain"}, {"text": "walked to the store"},
             {"text": "all alone and lost"}]
    assert stage_view._track_mood(42, lines) == "sad"
    assert stage_view._track_mood(None, lines) == "neutral"
