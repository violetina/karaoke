"""Find with songrec: radio reuse, songrec fallback, library match, queueing."""
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from karaoke import song_finder


@pytest.fixture()
def library(monkeypatch):
    """A fake library: title -> (track_id, pending steps)."""
    songs = {}
    monkeypatch.setattr("karaoke.localcache.connect", lambda *a, **k: nullcontext(MagicMock()))
    monkeypatch.setattr("karaoke.localcache.find_track_id_relaxed",
                        lambda artist, title, conn: songs.get(title, (None, []))[0])
    monkeypatch.setattr("karaoke.postprocess_queue.needs_postprocessing",
                        lambda tid, conn: next(p for t, p in songs.values() if t == tid))
    return songs


@pytest.fixture()
def queue(monkeypatch):
    sent = []
    monkeypatch.setattr("karaoke.tasks.enqueue_postprocess", lambda payload: sent.append(payload) or "id")
    return sent


@pytest.fixture()
def no_radio(monkeypatch):
    monkeypatch.setattr("karaoke.live_state.read", lambda name, **k: None)


def _radio(monkeypatch, **state):
    data = {"source": "radio", "artist": "", "title": "", **state}
    monkeypatch.setattr("karaoke.live_state.read",
                        lambda name, **k: data if name == "radio" else None)


def test_radio_answer_is_reused_without_running_songrec(monkeypatch, library, queue):
    library["Sweet Charity"] = (173, [])
    _radio(monkeypatch, artist="Mr. Bungle", title="Sweet Charity")
    with patch.object(song_finder, "_listen") as listen:
        out = song_finder.identify()
    listen.assert_not_called()
    assert out == {"artist": "Mr. Bungle", "title": "Sweet Charity", "track_id": 173,
                   "pending": [], "status": "found", "via": "radio"}
    assert queue == []


def test_radio_still_listening_does_not_compete_for_the_mic(monkeypatch, library):
    _radio(monkeypatch)
    with patch.object(song_finder, "_listen") as listen:
        out = song_finder.identify()
    listen.assert_not_called()
    assert out["status"] == "listening"


def test_songrec_result_is_matched(monkeypatch, library, queue, no_radio):
    library["Archangel"] = (1371, ["timings"])            # lyric steps do not block
    ref = SimpleNamespace(artist="Burial", title="Archangel")
    monkeypatch.setattr(song_finder, "_listen", lambda source: (ref, "output"))
    out = song_finder.identify()
    assert (out["status"], out["track_id"], out["via"]) == ("found", 1371, "output")
    assert queue == []


def test_songrec_miss_is_a_result_not_an_error(monkeypatch, library, no_radio):
    monkeypatch.setattr(song_finder, "_listen", lambda source: (None, "mic"))
    assert song_finder.identify()["status"] == "no_match"


def test_a_known_song_missing_chords_is_queued_in_full(library, queue):
    library["Murmur"] = (8823, ["chords", "timings"])
    out = song_finder.locate("John Frusciante", "Murmur")
    assert out["status"] == "queued" and out["pending"] == ["chords"]
    assert queue == [{"artist": "John Frusciante", "title": "Murmur", "url": "", "full": True}]


def test_an_unknown_song_is_queued_for_adding(library, queue):
    out = song_finder.locate("Nobody", "Unheard")
    assert out["status"] == "queued" and out["track_id"] is None
    assert len(queue) == 1


def test_a_second_identification_while_listening_is_refused(no_radio):
    song_finder._identify_lock.acquire()
    try:
        with pytest.raises(song_finder.Busy):
            song_finder.identify()
    finally:
        song_finder._identify_lock.release()


def test_status_reports_ready_once_graph_steps_are_done(library):
    assert song_finder.status("Nobody", "Unheard")["ready"] is False
    library["Archangel"] = (1371, ["chords"])
    assert song_finder.status("Burial", "Archangel") == {"track_id": 1371, "ready": False,
                                                         "pending": ["chords"]}
    library["Archangel"] = (1371, ["sync"])
    assert song_finder.status("Burial", "Archangel")["ready"] is True


def test_now_playing_falls_back_to_radio(monkeypatch):
    from karaoke import mcp_server

    monkeypatch.setattr("karaoke.playerctl.playing_player", lambda: None)
    monkeypatch.setattr("karaoke.live_state.current",
                        lambda **k: {"source": "radio", "artist": "Mr. Bungle",
                                     "title": "Sweet Charity"})
    monkeypatch.setattr("karaoke.playerctl.current_metadata",
                        lambda *a: pytest.fail("paused player consulted before radio"))
    cur = MagicMock()
    cur.fetchone.return_value = None
    conn = MagicMock()
    conn.cursor.return_value = cur
    monkeypatch.setattr("karaoke.localcache.connect", lambda *a, **k: nullcontext(conn))
    monkeypatch.setattr("karaoke.localcache.find_track_id_relaxed", lambda a, t, c: 173)
    out = json.loads(mcp_server.get_now_playing())
    assert (out["status"], out["player"], out["track_id"]) == ("playing", "radio", 173)


@pytest.fixture()
def http():
    from starlette.testclient import TestClient

    from karaoke.mcp_server import create_asgi_app

    return TestClient(create_asgi_app())


def test_identify_endpoint_returns_409_while_busy(http, monkeypatch):
    def busy(source):
        raise song_finder.Busy("songrec is already listening")
    monkeypatch.setattr(song_finder, "identify", busy)
    assert http.post("/api/identify").status_code == 409


def test_identify_endpoint_rejects_an_unknown_source(http):
    assert http.post("/api/identify?source=telepathy").status_code == 400


def test_identify_status_endpoint(http, monkeypatch):
    monkeypatch.setattr(song_finder, "status",
                        lambda a, t: {"track_id": 5, "ready": True, "pending": []})
    assert http.get("/api/identify/status?artist=a&title=t").json()["ready"] is True


def test_locate_endpoint_places_a_named_song(http, monkeypatch):
    seen = []
    monkeypatch.setattr(song_finder, "locate",
                        lambda a, t: seen.append((a, t)) or {"status": "found", "track_id": 7,
                                                             "pending": []})
    resp = http.post("/api/locate", json={"artist": "Alien Ant Farm", "title": "Smooth Criminal"})
    assert resp.json()["track_id"] == 7
    assert seen == [("Alien Ant Farm", "Smooth Criminal")]


def test_locate_endpoint_requires_a_title(http):
    assert http.post("/api/locate", json={"artist": "x"}).status_code == 400


def test_graph_page_offers_follow():
    from karaoke.graph_view import GRAPH_HTML as page

    assert 'id="follow-btn"' in page and "function toggleFollow" in page


def test_ensure_processed_queues_the_full_chain_for_a_new_song(library, queue, monkeypatch):
    light = []
    monkeypatch.setattr("karaoke.postprocess_queue.enqueue_if_needed",
                        lambda *a, **k: light.append(a) or True)
    assert song_finder.ensure_processed("DJ Agent 86", "Another One Bites The Dust") == "queued-full"
    assert queue[0]["full"] is True
    assert light == []                      # never both chains for one song


def test_ensure_processed_leaves_lyric_gaps_to_the_light_chain(library, queue, monkeypatch):
    library["Drive"] = (18955, ["timings"])
    light = []
    monkeypatch.setattr("karaoke.postprocess_queue.enqueue_if_needed",
                        lambda *a, **k: light.append(a) or True)
    assert song_finder.ensure_processed("Incubus", "Drive") == "queued"
    assert queue == [] and light == [("Incubus", "Drive", "")]


def test_ensure_processed_on_a_complete_song_does_nothing(library, queue, monkeypatch):
    library["Drive"] = (18955, [])
    monkeypatch.setattr("karaoke.postprocess_queue.enqueue_if_needed", lambda *a, **k: False)
    assert song_finder.ensure_processed("Incubus", "Drive") == "complete"
    assert queue == []


def test_get_synced_can_leave_queueing_to_the_caller(monkeypatch):
    from karaoke import player
    from karaoke.identify import SongRef
    from karaoke.lyrics import Lyrics

    ly = Lyrics(synced_raw="[00:01.00] hi", plain="hi", source="lrclib")
    monkeypatch.setattr("karaoke.localcache.connect", lambda *a, **k: nullcontext(MagicMock()))
    monkeypatch.setattr("karaoke.localcache.find_track_id", lambda *a, **k: None)
    monkeypatch.setattr("karaoke.localcache.add_track_and_lyrics", lambda *a, **k: None)
    monkeypatch.setattr(player, "fetch_lrclib", lambda *a, **k: ly)
    called = []
    monkeypatch.setattr("karaoke.postprocess_queue.enqueue_if_needed",
                        lambda *a, **k: called.append(a))
    player.get_synced(SongRef(artist="a", title="t"), queue_postprocess=False)
    assert called == []
    player.get_synced(SongRef(artist="a", title="t"))
    assert called
