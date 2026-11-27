"""DJ chat RAG: the playing track's key / BPM / mood / chords reach the prompt."""
import json
import time

from psycopg.types.json import Jsonb

from karaoke import chord_timeline, dashboard_api, localcache, mood_timeline, track_analysis


def _seed_track() -> int:
    with localcache.connect() as conn:
        track_analysis.ensure_schema(conn)
        mood_timeline.ensure_schema(conn)
        chord_timeline.ensure_schema(conn)
        tid = conn.execute(
            "INSERT INTO tracks (artist, title, album, duration) VALUES ('The Slits', 'Typical Girls', '', 200)"
            " RETURNING track_id").fetchone()["track_id"]
        conn.execute("INSERT INTO track_analysis (track_id, detected_key, bpm, energy, updated_at)"
                     " VALUES (%s, 'F minor', 161.7, 0.4, %s)", (tid, time.time()))
        conn.execute("INSERT INTO track_moods (track_id, mood_version, segments, summary, updated_at)"
                     " VALUES (%s, 1, '[]'::jsonb, %s::jsonb, %s)",
                     (tid, json.dumps({"dominant": "happy", "dominant_feeling": "cheerful"}), time.time()))
        chords = [{"at": 0, "end": 4, "name": "Fm"}, {"at": 4, "end": 5, "name": "C#"},
                  {"at": 5, "end": 7, "name": "G#"}, {"at": 7, "end": 8, "name": "N"}]
        conn.execute("INSERT INTO track_chords (track_id, chords_version, duration_s, chords, harmony,"
                     " source_path, updated_at) VALUES (%s, 2, 8, %s, %s, '', %s)",
                     (tid, Jsonb(chords), Jsonb({"style": "mixed"}), time.time()))
        conn.commit()
    return tid


def test_current_track_analysis_fact_has_key_bpm_mood_chords():
    _seed_track()
    fact = dashboard_api._current_track_analysis_fact("The Slits", "Typical Girls")
    assert "Key: F minor (Camelot 4A)" in fact
    assert "BPM: 162" in fact
    assert "Dominant Mood: happy" in fact and "Feeling: cheerful" in fact
    # most-used first, the no-chord marker dropped
    assert "Most-used chords: Fm, G#, C#" in fact
    assert "Harmony style: mixed" in fact


def test_current_track_analysis_fact_resolves_spelling_variants_and_misses():
    _seed_track()
    assert "F minor" in dashboard_api._current_track_analysis_fact("the slits", "Typical Girls")
    assert dashboard_api._current_track_analysis_fact("Nobody", "Nothing") == ""
    assert dashboard_api._current_track_analysis_fact("", "") == ""


def test_retrieve_query_facts_includes_analysis_for_key_question(monkeypatch):
    _seed_track()

    def no_opensearch():
        raise RuntimeError("no opensearch in tests")

    monkeypatch.setattr("karaoke.osclient.client", no_opensearch)
    facts = dashboard_api._retrieve_query_facts("What key and BPM is this?", "The Slits", "Typical Girls")
    assert "Key: F minor" in facts and "BPM: 162" in facts


def _capture_prompt(monkeypatch):
    seen = {}

    def fake_prompt(now_artist, now_title, artist_context="", query_facts="", *, tool_capable=False):
        seen["artist"], seen["title"] = now_artist, now_title
        return "system"

    async def fake_stream(provider, model, system_prompt, user_msg, timeout=60.0):
        yield "ok"

    def no_opensearch():
        raise RuntimeError("no opensearch in tests")

    monkeypatch.setattr(dashboard_api, "_dj_chat_system_prompt", fake_prompt)
    monkeypatch.setattr("karaoke.ai_providers.stream_chat", fake_stream)
    monkeypatch.setattr("karaoke.osclient.client", no_opensearch)
    return seen


def _chat(artist, title):
    from fastapi.testclient import TestClient
    with TestClient(dashboard_api.app) as client:
        r = client.post("/api/dj/chat", json={"message": "hi", "current_artist": artist,
                                               "current_title": title, "provider": "ollama"})
        assert r.status_code == 200
        r.read()


def test_known_track_is_not_rewritten_to_an_artist_named_in_its_title(monkeypatch):
    """'Typical Girls' contains the band name 'Girls'; the playing song must stay The Slits."""
    _seed_track()
    with localcache.connect() as conn:
        conn.execute("INSERT INTO tracks (artist, title, album, duration) VALUES ('Girls', 'Heartbreaker', '', 180)")
        conn.commit()
    seen = _capture_prompt(monkeypatch)
    _chat("The Slits", "Typical Girls")
    assert (seen["artist"], seen["title"]) == ("The Slits", "Typical Girls")


def test_unknown_uploader_pair_is_still_repaired(monkeypatch):
    with localcache.connect() as conn:
        conn.execute("INSERT INTO tracks (artist, title, album, duration) VALUES ('Mogwai', 'Auto Rock', '', 180)")
        conn.commit()
    seen = _capture_prompt(monkeypatch)
    _chat("SomeUploader99", "Mogwai - Auto Rock (Official)")
    assert seen["title"] == "Auto Rock"
