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


def test_analysis_only_query_skips_fuzzy_opensearch_noise(monkeypatch):
    _seed_track()
    calls = []

    class FakeOS:
        def search(self, index, body):
            calls.append(index)
            return {"hits": {"hits": [{"_source": {"name": "Unrelated Band", "artist": "X", "title": "Y", "track_id": 1}}]}}

    monkeypatch.setattr("karaoke.osclient.client", lambda: FakeOS())
    facts = dashboard_api._retrieve_query_facts("What key and BPM is this?", "The Slits", "Typical Girls")
    assert facts.startswith("Musical analysis")
    assert "Unrelated Band" not in facts and "karaoke-artist-cache" not in calls


def test_suggest_facts_are_ready_to_copy_queue_lines(monkeypatch):
    import json as _json
    monkeypatch.setattr("karaoke.osclient.client", lambda: (_ for _ in ()).throw(RuntimeError("no os")))
    monkeypatch.setattr("karaoke.mcp_server.suggest_next_tracks",
                        lambda artist, title, limit=3: _json.dumps({"suggestions": [
                            {"artist": "Bowie", "title": "Heroes", "reason": "fits"},
                            {"artist": "Pulp", "title": "Common People", "reason": "fits"}]}))
    facts = dashboard_api._retrieve_query_facts("Suggest the next song.", "The Slits", "Typical Girls")
    assert "RECOMMENDED NEXT SONGS" in facts
    assert "[QUEUE: Bowie - Heroes]" in facts and "copy EXACTLY ONE" in facts


def test_prompt_leads_with_facts_and_caps_queue(monkeypatch):
    monkeypatch.setattr(dashboard_api, "_retrieve_query_facts", lambda *a, **k: "")
    facts = "Musical analysis for current track 'A - B': Key: F minor (Camelot 4A) · BPM: 162"
    prompt = dashboard_api._dj_chat_system_prompt("A", "B", query_facts=facts)
    # facts appear before the behavioural rules, not buried at the end
    assert prompt.index("VERIFIED FACTS") < prompt.index("CRITICAL FORMAT RULE")
    assert "AT MOST ONE [QUEUE" in prompt
    # the random sample-track distractor is dropped once real recs are present
    recs = "RECOMMENDED NEXT SONGS (…):\n  1. [QUEUE: Bowie - Heroes]  (fits)"
    assert "valid songs in the library right now" not in dashboard_api._dj_chat_system_prompt("A", "B", query_facts=recs)


class _OneHitOS:
    """Artist-cache stub that returns one fixed doc for any query."""

    def __init__(self, doc=None):
        self.doc = doc

    def search(self, index, body):
        hits = [{"_source": self.doc}] if (self.doc and index == "karaoke-artist-cache") else []
        return {"hits": {"hits": hits}}


def test_artist_missing_from_cache_still_counts_as_playing(monkeypatch):
    """A duo/feat. credit with no artist-cache doc must not become 'nothing playing'."""
    seen = _capture_prompt(monkeypatch)
    monkeypatch.setattr("karaoke.osclient.client", lambda: _OneHitOS(None))
    _chat("Britney Spears & Iggy Azalea", "Pretty Girls")
    assert (seen["artist"], seen["title"]) == ("Britney Spears & Iggy Azalea", "Pretty Girls")


def test_fuzzy_artist_cache_hit_does_not_rename_playing_artist(monkeypatch):
    seen = _capture_prompt(monkeypatch)
    monkeypatch.setattr("karaoke.osclient.client",
                        lambda: _OneHitOS({"name": "Britney Spears", "aliases": [], "member_names": []}))
    _chat("Britney Spears & Iggy Azalea", "Pretty Girls")
    assert seen["artist"] == "Britney Spears & Iggy Azalea"


def test_artist_cache_alias_still_canonicalises(monkeypatch):
    seen = _capture_prompt(monkeypatch)
    monkeypatch.setattr("karaoke.osclient.client",
                        lambda: _OneHitOS({"name": "The Slits", "aliases": ["Slits"], "member_names": ["Ari Up"]}))
    _chat("Slits", "Typical Girls")
    assert seen["artist"] == "The Slits"


def test_suggest_with_recs_skips_fuzzy_name_matches(monkeypatch):
    import json as _json
    calls = []

    class FakeOS:
        def search(self, index, body):
            calls.append(index)
            return {"hits": {"hits": [{"_source": {"name": "Earth to Eve", "artist": "Earth to Eve",
                                                   "title": "Toy Soldier", "track_id": 9}}]}}

    monkeypatch.setattr("karaoke.osclient.client", lambda: FakeOS())
    monkeypatch.setattr("karaoke.mcp_server.suggest_next_tracks",
                        lambda artist, title, limit=3: _json.dumps({"suggestions": [
                            {"artist": "Bowie", "title": "Heroes", "reason": "fits"}]}))
    facts = dashboard_api._retrieve_query_facts("Suggest the next song to play after this one.",
                                                "The Slits", "Typical Girls")
    assert "[QUEUE: Bowie - Heroes]" in facts
    assert "Earth to Eve" not in facts and calls == []
