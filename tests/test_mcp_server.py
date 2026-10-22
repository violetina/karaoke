"""Tests for the Model Context Protocol (MCP) server for the Karaoke AI DJ."""
from __future__ import annotations

import inspect
import json
import time
import pytest

from karaoke import mcp_server, player_open, ytmusic_playlist


@pytest.fixture(autouse=True)
def _no_live_ytmusic(monkeypatch):
    """Stub remote YouTube Music API calls so tests never mutate the user's real YTMusic playlists."""
    monkeypatch.setattr(
        ytmusic_playlist,
        "resolve_or_create_dj_playlist",
        lambda name="Karaoke: DJ List", client=None, conn=None, playlist_key="dj-list": (
            playlist_key or "dj-list",
            "",
        ),
    )
    monkeypatch.setattr(
        ytmusic_playlist,
        "add_track_to_ytmusic_playlist",
        lambda playlist_id, artist, title, video_id="", client=None: (True, video_id or "mockvid12345"),
    )
    monkeypatch.setattr(
        ytmusic_playlist,
        "clear_ytmusic_playlist",
        lambda playlist_id, client=None: True,
    )


def test_mcp_server_definition():
    """Verify the MCP server is properly constructed with tools."""
    assert mcp_server.dj_mcp.name == "karaoke-dj"
    tool_names = [t.name for t in mcp_server.dj_mcp._tool_manager.list_tools()]
    assert "search_songs" in tool_names
    assert "get_now_playing" in tool_names
    assert "suggest_next_tracks" in tool_names
    assert "analyze_lyric_vibe" in tool_names
    assert "get_dj_stats" in tool_names
    assert "play_track" in tool_names
    assert "get_similarity_graph" in tool_names
    assert "list_playlists" in tool_names
    assert "search_by_chords" in tool_names
    assert "get_track_chords" in tool_names
    assert "scan_track" in tool_names


def test_search_songs():
    """Verify search_songs returns structured JSON."""
    res_str = mcp_server.search_songs(query="", limit=3)
    data = json.loads(res_str)
    assert "count" in data
    assert "tracks" in data
    assert isinstance(data["tracks"], list)
    if data["tracks"]:
        track = data["tracks"][0]
        assert "track_id" in track
        assert "artist" in track
        assert "title" in track
        assert "camelot" in track


def test_search_songs_by_key():
    """Verify search_songs filters by key/Camelot code."""
    res_str = mcp_server.search_songs(key="8A", limit=2)
    data = json.loads(res_str)
    for track in data["tracks"]:
        assert track["camelot"] == "8A"


def test_get_now_playing():
    """Verify get_now_playing handles desktop media probe gracefully."""
    res_str = mcp_server.get_now_playing()
    data = json.loads(res_str)
    assert data["status"] in ("playing", "idle")


def test_suggest_next_tracks():
    """Verify suggest_next_tracks harmonic flow recommendation."""
    # Search for an analyzed track first to act as a seed
    search_res = json.loads(mcp_server.search_songs(limit=10))
    seed = next((t for t in search_res["tracks"] if t.get("camelot") and t["camelot"] != "?"), None)
    if seed:
        res_str = mcp_server.suggest_next_tracks(track_id=seed["track_id"], strategy="harmonic", limit=3)
        data = json.loads(res_str)
        assert "seed" in data
        assert "suggestions" in data
        assert data["seed"]["track_id"] == seed["track_id"]


def test_analyze_lyric_vibe():
    """Verify analyze_lyric_vibe returns mood arc and tips."""
    search_res = json.loads(mcp_server.search_songs(only_synced_lyrics=True, limit=1))
    if search_res["tracks"]:
        track_id = search_res["tracks"][0]["track_id"]
        res_str = mcp_server.analyze_lyric_vibe(track_id)
        data = json.loads(res_str)
        assert "dominant_mood" in data
        assert "dj_singer_tips" in data


def test_get_dj_stats():
    """Verify get_dj_stats returns overall library health and top favorites."""
    res_str = mcp_server.get_dj_stats()
    data = json.loads(res_str)
    assert "plays" in data
    assert "top_tracks" in data
    assert "top_artists" in data


@pytest.fixture
def launcher(monkeypatch):
    """Stub open_song_url, recording calls and validating them for real.

    play_track imports open_song_url inside the function body, so the name
    must be patched on player_open rather than on mcp_server. Every captured
    call is bound against the genuine signature, so an argument list the real
    function would reject still raises TypeError here.
    """
    real_signature = inspect.signature(player_open.open_song_url)
    calls: list[inspect.BoundArguments] = []
    returns = []

    def fake_open_song_url(*args, **kwargs):
        calls.append(real_signature.bind(*args, **kwargs))
        return returns.pop(0) if returns else None

    monkeypatch.setattr(player_open, "open_song_url", fake_open_song_url)
    return calls, returns


@pytest.fixture
def playable_track() -> int:
    """Insert one track with a YouTube source and return its id.

    The autouse clear_db fixture empties every table before each test, so a
    test that needs a row has to create it.
    """
    from karaoke import localcache

    track_id = 4242
    with localcache.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tracks (track_id, artist, title, album, duration, play_count)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (track_id, "The Doors", "Wild Child", "The Soft Parade", 100.0, 0),
        )
        cur.execute(
            "INSERT INTO sources (source_id, track_id, kind, url) VALUES (%s, %s, %s, %s)",
            (1, track_id, "youtube", "https://www.youtube.com/watch?v=USzJmEUzq6M"),
        )
    return track_id


def test_play_track_passes_source_kind(launcher, playable_track):
    """play_track must supply the `kind` argument open_song_url requires."""
    calls, _ = launcher

    mcp_server.play_track(playable_track)

    assert len(calls) == 1
    bound = calls[0]
    bound.apply_defaults()
    # `kind` is positional-or-keyword and has no default: omitting it used to
    # raise TypeError before the call ever reached the player.
    assert "kind" in bound.arguments
    # Artist and title are what let YouTube Music resolve the canonical audio
    # track rather than a video edit with a different intro.
    assert bound.arguments["artist"]
    assert bound.arguments["title"]


def test_play_track_reports_launched_without_pid(launcher, playable_track):
    """A None return is the kiosk/CDP success path, not a failure."""
    calls, returns = launcher

    returns.append(None)
    data = json.loads(mcp_server.play_track(playable_track))

    assert data["status"] == "launched"
    assert data["pid"] is None
    assert len(calls) == 1


def test_play_track_surfaces_pid(launcher, playable_track):
    """The xdg-open path returns a pid, which callers should see."""
    _, returns = launcher

    returns.append(31337)
    data = json.loads(mcp_server.play_track(playable_track))

    assert data["status"] == "launched"
    assert data["pid"] == 31337


def test_play_track_unknown_track(launcher):
    """An unknown id reports an error and never reaches the player."""
    calls, _ = launcher

    data = json.loads(mcp_server.play_track(-1))

    assert "error" in data
    assert calls == []


def test_create_asgi_app_endpoints():
    """Verify create_asgi_app handles HEAD /sse, POST /sse (streamable HTTP), and /health."""
    from starlette.testclient import TestClient

    app = mcp_server.create_asgi_app()
    with TestClient(app) as client:
        # 1. Healthcheck
        r_health = client.get("/health")
        assert r_health.status_code == 200
        assert r_health.json() == {"status": "ok"}

        # 2. HEAD /sse (systemd readiness check)
        r_head = client.head("/sse")
        assert r_head.status_code == 200

        # 3. POST /sse (Streamable HTTP initialize sent by Obot)
        init_payload = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "obot-test", "version": "1.0"},
            },
        }
        r_post = client.post("/sse", json=init_payload)
        assert r_post.status_code == 200
        assert "capabilities" in r_post.text


def test_dj_playlist_tools(playable_track):
    """Verify add_to_dj_playlist, get_dj_playlist, and clear_dj_playlist tools."""
    # 1. Clear playlist initially
    r_clear = json.loads(mcp_server.clear_dj_playlist())
    assert r_clear["ok"] is True

    # 2. Add track by ID
    r_add = json.loads(mcp_server.add_to_dj_playlist(track_id=playable_track))
    assert r_add["ok"] is True
    assert r_add["playlist_id"] in ("dj-list", "PLeaH9ITPDGQo") or r_add["playlist_id"].startswith("PL")
    assert r_add["position"] == 1
    assert r_add["track"]["track_id"] == playable_track
    assert r_add["track"]["artist"] == "The Doors"

    # 3. Retrieve playlist
    r_get = json.loads(mcp_server.get_dj_playlist())
    assert r_get["playlist_id"] in ("dj-list", "PLeaH9ITPDGQo") or r_get["playlist_id"].startswith("PL")
    assert r_get["track_count"] == 1
    assert len(r_get["tracks"]) == 1
    assert r_get["tracks"][0]["title"] == "Wild Child"

    # 4. Clear playlist and verify empty
    r_clear2 = json.loads(mcp_server.clear_dj_playlist())
    assert r_clear2["ok"] is True
    r_get2 = json.loads(mcp_server.get_dj_playlist())
    assert r_get2["track_count"] == 0
    assert r_get2["tracks"] == []



def test_add_to_dj_playlist_is_idempotent(playable_track):
    """Re-adding a track reports it as present rather than duplicating it."""
    mcp_server.clear_dj_playlist()

    first = json.loads(mcp_server.add_to_dj_playlist(track_id=playable_track))
    assert first["ok"] is True
    assert not first.get("already_present")

    second = json.loads(mcp_server.add_to_dj_playlist(track_id=playable_track))
    assert second["ok"] is True
    assert second["already_present"] is True

    listing = json.loads(mcp_server.get_dj_playlist())
    assert listing["track_count"] == 1


def test_suggest_next_tracks_excludes_playlist_members(playable_track, monkeypatch):
    """A song already in the DJ list is never offered as the next track."""
    # Two analysed tracks that suggest_next_tracks can actually return.
    from karaoke import localcache

    with localcache.connect() as conn:
        cur = conn.cursor()
        for tid, title, bpm, key in (
            (5001, "Seed Song", 120.0, "A minor"),
            (5002, "Queued Song", 121.0, "A minor"),
            (5003, "Fresh Song", 122.0, "A minor"),
        ):
            cur.execute(
                "INSERT INTO tracks (track_id, artist, title, album, duration, play_count)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (tid, "Test Artist", title, "", 180.0, 0),
            )
            cur.execute(
                "INSERT INTO track_analysis (track_id, detected_key, bpm, energy, updated_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (tid, key, bpm, 0.5, time.time()),
            )

    mcp_server.clear_dj_playlist()
    mcp_server.add_to_dj_playlist(track_id=5002)

    data = json.loads(mcp_server.suggest_next_tracks(track_id=5001, limit=10))
    returned = [s["track_id"] for s in data["suggestions"]]
    assert 5002 not in returned, "queued song must not be suggested"
    assert 5003 in returned, "unqueued song should still be offered"

    # Opting out restores the old behaviour.
    allowed = json.loads(
        mcp_server.suggest_next_tracks(track_id=5001, limit=10, exclude_playlist="")
    )
    assert 5002 in [s["track_id"] for s in allowed["suggestions"]]


@pytest.fixture
def paging_library():
    """Ten analysed tracks, enough to page through."""
    from karaoke import localcache

    with localcache.connect() as conn:
        cur = conn.cursor()
        for n in range(10):
            tid = 6000 + n
            cur.execute(
                "INSERT INTO tracks (track_id, artist, title, album, duration, play_count)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (tid, f"Artist {n:02d}", f"Song {n:02d}", "", 180.0, 100 - n),
            )
            cur.execute(
                "INSERT INTO track_analysis (track_id, detected_key, bpm, energy, updated_at)"
                " VALUES (%s, %s, %s, %s, %s)",
                (tid, "A minor", 120.0, 0.5, time.time()),
            )
    return [6000 + n for n in range(10)]


def test_search_songs_pages_without_overlap(paging_library):
    first = json.loads(mcp_server.search_songs(limit=4, offset=0))
    second = json.loads(mcp_server.search_songs(limit=4, offset=4))

    ids1 = [t["track_id"] for t in first["tracks"]]
    ids2 = [t["track_id"] for t in second["tracks"]]
    assert len(ids1) == 4 and len(ids2) == 4
    assert not set(ids1) & set(ids2), "pages must not repeat tracks"
    assert second["offset"] == 4


def test_search_songs_reports_more_pages(paging_library):
    """has_more needs a row beyond the window, not just a full page.

    The first implementation stopped collecting exactly at the window, so
    has_more was always False even with results left.
    """
    early = json.loads(mcp_server.search_songs(limit=4, offset=0))
    assert early["has_more"] is True

    last = json.loads(mcp_server.search_songs(limit=4, offset=8))
    assert last["has_more"] is False


def test_search_songs_offset_is_clamped(paging_library):
    data = json.loads(mcp_server.search_songs(limit=3, offset=-5))
    assert data["offset"] == 0
    assert len(data["tracks"]) == 3


def test_suggest_structural_strategy(paging_library, monkeypatch):
    """Verify suggest_next_tracks supports 'structural' harmonic root-motion strategy."""
    from karaoke import search
    monkeypatch.setattr(search, "similar_chords_for_track", lambda track_id, k: [
        {"track_id": 9999, "artist": "Jazz Artist", "title": "Jazz Song", "score": 0.95, "form": "modulating"}
    ])
    res_str = mcp_server.suggest_next_tracks(track_id=paging_library[0], strategy="structural", limit=3)
    data = json.loads(res_str)
    assert data["strategy"] == "structural_chord_motion"
    assert "seed" in data
    assert "suggestions" in data
    assert len(data["suggestions"]) == 1


def test_get_similarity_graph(paging_library, monkeypatch):
    """Verify get_similarity_graph returns nodes, edges, and valid web graph URL."""
    from karaoke import search
    monkeypatch.setattr(search, "similar_chords_for_track", lambda track_id, k: [
        {"track_id": paging_library[1], "artist": "Artist 01", "title": "Song 01", "score": 0.95, "form": "static"}
    ])
    res_str = mcp_server.get_similarity_graph(track_id=paging_library[0], strategy="structural", hops=2)
    data = json.loads(res_str)
    assert "nodes" in data
    assert "edges" in data
    assert "stats" in data
    assert "graph_url" in data
    assert data["stats"]["node_count"] >= 1
    assert "http://localhost:8888/graph" in data["graph_url"]


def test_suggest_context_strategy(paging_library, monkeypatch):
    """Verify suggest_next_tracks supports 'context' semantic lyrics strategy."""
    from karaoke import search
    monkeypatch.setattr(search, "similar_lyrics_for_track", lambda track_id, k: [
        {"track_id": paging_library[1], "artist": "Lyricist", "title": "Poetic Song", "score": 0.88, "space": "lyrics_semantic"}
    ])
    res_str = mcp_server.suggest_next_tracks(track_id=paging_library[0], strategy="context", limit=3)
    data = json.loads(res_str)
    assert data["strategy"] == "context_lyrics_semantic"
    assert len(data["suggestions"]) == 1


def test_suggest_mood_strategy(paging_library, monkeypatch):
    """Verify suggest_next_tracks supports 'mood' emotional feeling strategy."""
    from karaoke import search
    monkeypatch.setattr(search, "similar_mood_for_track", lambda track_id, k: [
        {"track_id": paging_library[1], "artist": "Emotive Artist", "title": "Sad Ballad", "score": 0.92, "dominant_mood": "sad", "space": "sentiment_mood"}
    ])
    # The mood-timeline engine (dj_suggest) answers first; the lexicon-based
    # path is the fallback when it finds nothing.
    from karaoke import dj_suggest
    monkeypatch.setattr(dj_suggest, "suggest", lambda *a, **k: [])
    res_str = mcp_server.suggest_next_tracks(track_id=paging_library[0], strategy="mood", limit=3)
    data = json.loads(res_str)
    assert data["strategy"] == "mood_emotion_sentiment"
    assert len(data["suggestions"]) == 1


def test_suggest_mood_uses_the_mood_timeline_engine_first(paging_library, monkeypatch):
    from karaoke import dj_suggest
    monkeypatch.setattr(dj_suggest, "suggest", lambda seed, strategy, **k: [
        {"track_id": paging_library[1], "artist": "A", "title": "T", "reason": "romantic → dreamy"}])
    data = json.loads(mcp_server.suggest_next_tracks(track_id=paging_library[0], strategy="mood", limit=3))
    assert data["strategy"] == "mood"
    assert data["suggestions"][0]["reason"] == "romantic → dreamy"


def test_get_similarity_graph_context_and_mood(paging_library, monkeypatch):
    """Verify get_similarity_graph builds graphs for context and mood strategies."""
    from karaoke import search
    monkeypatch.setattr(search, "similar_lyrics_for_track", lambda track_id, k: [
        {"track_id": paging_library[1], "artist": "Artist 01", "title": "Song 01", "score": 0.85, "space": "lyrics_semantic"}
    ])
    monkeypatch.setattr(search, "similar_mood_for_track", lambda track_id, k: [
        {"track_id": paging_library[1], "artist": "Artist 01", "title": "Song 01", "score": 0.89, "dominant_mood": "tender", "space": "sentiment_mood"}
    ])

    ctx_graph = json.loads(mcp_server.get_similarity_graph(track_id=paging_library[0], strategy="context", hops=1))
    assert ctx_graph["stats"]["strategy"] == "context"
    assert len(ctx_graph["nodes"]) >= 1

    mood_graph = json.loads(mcp_server.get_similarity_graph(track_id=paging_library[0], strategy="mood", hops=1))
    assert mood_graph["stats"]["strategy"] == "mood"
    assert len(mood_graph["nodes"]) >= 1


def test_api_play_endpoint(paging_library, monkeypatch):
    """Verify ASGI route /api/play accepts both GET and POST requests."""
    from starlette.testclient import TestClient
    app = mcp_server.create_asgi_app()
    client = TestClient(app)

    # Mock play_track to avoid spawning actual desktop browser during test
    monkeypatch.setattr(mcp_server, "play_track", lambda track_id: json.dumps({
        "status": "launched",
        "track_id": track_id,
        "title": "Mock Song",
        "artist": "Mock Artist",
    }))

    # 1. POST with query parameter (as sent by Cytoscape UI)
    post_res = client.post(f"/api/play?track_id={paging_library[0]}")
    assert post_res.status_code == 200
    assert post_res.json()["status"] == "launched"

    # 2. POST with JSON body
    post_json = client.post("/api/play", json={"track_id": paging_library[0]})
    assert post_json.status_code == 200
    assert post_json.json()["status"] == "launched"

    # 3. GET with query param
    get_res = client.get(f"/api/play?track_id={paging_library[0]}")
    assert get_res.status_code == 200
    assert get_res.json()["status"] == "launched"


def test_api_playlists_endpoint():
    """Verify ASGI route /api/playlists returns JSON list of saved playlists."""
    from starlette.testclient import TestClient
    app = mcp_server.create_asgi_app()
    client = TestClient(app)

    res = client.get("/api/playlists")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)


def test_get_similarity_graph_filters(paging_library, monkeypatch):
    """Verify get_similarity_graph respects filter options like min_score and bpm."""
    from karaoke import search
    monkeypatch.setattr(search, "similar_chords_for_track", lambda track_id, k: [
        {"track_id": paging_library[1], "artist": "Artist 01", "title": "Song 01", "score": 0.45, "space": "chord_motion"},
        {"track_id": paging_library[2], "artist": "Artist 02", "title": "Song 02", "score": 0.88, "space": "chord_motion"},
    ])

    # Filter with min_score=0.60: edge with score 0.45 should be omitted
    graph_data = json.loads(mcp_server.get_similarity_graph(
        track_id=paging_library[0],
        strategy="structural",
        hops=1,
        min_score=0.60,
    ))
    assert graph_data["stats"]["min_score"] == 0.60
    for edge in graph_data["edges"]:
        assert edge["weight"] >= 0.60


def test_custom_playlist_batch_add_and_list_playlists(paging_library):
    """Custom playlist slugs format cleanly, support batch track_ids, and appear in list_playlists."""
    res = json.loads(
        mcp_server.add_to_dj_playlist(
            track_ids=paging_library[:3],
            playlist_id="funny-happy",
        )
    )
    assert res["ok"] is True
    assert res["playlist_name"] == "Karaoke: Funny Happy"
    assert res["added_count"] == 3
    assert len(res["tracks"]) == 3

    got = json.loads(mcp_server.get_dj_playlist(playlist_id="funny-happy"))
    assert got["name"] == "Karaoke: Funny Happy"
    assert got["track_count"] == 3

    listed = json.loads(mcp_server.list_playlists(source_kind="dj"))
    names = [p["name"] for p in listed["playlists"]]
    assert "Karaoke: Funny Happy" in names


def test_search_by_chords_and_get_track_chords(paging_library):
    """Verify v2 chord progression search (Roman numerals & chord names) and get_track_chords."""
    from psycopg.types.json import Jsonb
    from karaoke import chord_timeline, localcache

    tid = paging_library[0]
    chords_rows = [
        {"at": 0.0, "end": 2.0, "name": "Dm7", "roman": "ii7"},
        {"at": 2.0, "end": 4.0, "name": "G7", "roman": "V7"},
        {"at": 4.0, "end": 8.0, "name": "Cmaj7", "roman": "Imaj7"},
        {"at": 8.0, "end": 10.0, "name": "Am7", "roman": "vi7"},
        {"at": 10.0, "end": 12.0, "name": "Dm7", "roman": "ii7"},
        {"at": 12.0, "end": 14.0, "name": "G7", "roman": "V7"},
        {"at": 14.0, "end": 18.0, "name": "Cmaj7", "roman": "Imaj7"},
    ]
    harmony_doc = {
        "style": "jazz-functional",
        "ii_v_i": 2,
        "minor_ii_v_i": 0,
        "ii_v": 2,
        "tritone_subs": 1,
        "secondary_dominants": 1,
        "backdoor": 0,
        "andalusian": 0,
        "dominant_share": 0.25,
        "blues_score": 0.05,
        "cadences_per_minute": 4.0,
        "fifth_chains_per_minute": 4.0,
        "key_tonic": 0,
        "key_minor": False,
    }
    with localcache.connect() as conn:
        chord_timeline.ensure_schema(conn)
        conn.execute(
            "INSERT INTO track_chords (track_id, chords_version, duration_s, chords, harmony, source_path, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (tid, 2, 18.0, Jsonb(chords_rows), Jsonb(harmony_doc), "", time.time()),
        )

    # 1. Search by Roman progression 'ii V I' (matches ii7 -> V7 -> Imaj7)
    by_roman = json.loads(mcp_server.search_by_chords(progression="ii V I"))
    assert by_roman["count"] == 1
    assert by_roman["tracks"][0]["track_id"] == tid
    assert by_roman["tracks"][0]["progression_matches"] == 2
    assert "Dm7 (ii7) -> G7 (V7) -> Cmaj7 (Imaj7)" in by_roman["tracks"][0]["matched_excerpt"]

    # 2. Search by chord names 'Dm G7 C' (matches Dm7 -> G7 -> Cmaj7; G7 is dominant, not tonic G)
    by_names = json.loads(mcp_server.search_by_chords(progression="Dm G7 C", style="jazz-functional", cadence="ii_v_i"))
    assert by_names["count"] == 1
    assert by_names["tracks"][0]["track_id"] == tid

    # 3. Loose/non-functional false positives must NOT match:
    # - D7 (I7) is a dominant 7th, not a major tonic 'I' or 'D'
    # - Em6 -> A -> D7 (ii6 -> V -> I7) is NOT a ii-V-I
    # - Plain triads Gm -> C -> F (ii -> V -> I without dominant 7th) and Gm -> C6 -> F (ii -> V6 -> I) are NOT ii-V-I cadences
    assert not mcp_server._roman_token_matches("I", "I7")
    assert not mcp_server._chord_token_matches("D", "D7")
    assert not mcp_server._roman_token_matches("ii", "ii6")
    assert not mcp_server._chord_token_matches("Em", "Em6")
    assert not mcp_server._roman_token_matches("V", "V6")

    false_positive_chords = [
        {"at": 0.0, "end": 2.0, "name": "Em6", "roman": "ii6"},
        {"at": 2.0, "end": 4.0, "name": "A", "roman": "V"},
        {"at": 4.0, "end": 6.0, "name": "D7", "roman": "I7"},
        {"at": 6.0, "end": 8.0, "name": "Gm", "roman": "ii"},
        {"at": 8.0, "end": 10.0, "name": "C", "roman": "V"},
        {"at": 10.0, "end": 12.0, "name": "F", "roman": "I"},
        {"at": 12.0, "end": 14.0, "name": "Gm", "roman": "ii"},
        {"at": 14.0, "end": 16.0, "name": "C6", "roman": "V6"},
        {"at": 16.0, "end": 18.0, "name": "F", "roman": "I"},
    ]
    fp_count, _, _ = mcp_server._match_progression_tokens(false_positive_chords, ["ii", "V", "I"])
    assert fp_count == 0

    # 4. Inspect full v2 chord timeline & recurring progressions
    detail = json.loads(mcp_server.get_track_chords(track_id=tid))
    assert detail["track_id"] == tid
    assert detail["chords_version"] == 2
    assert detail["harmony"]["style"] == "jazz-functional"
    assert detail["top_roman_progressions"][0]["roman"] == "ii7 -> V7 -> Imaj7"
    assert detail["top_roman_progressions"][0]["count"] == 2


def test_delete_playlist(paging_library, monkeypatch):
    """Verify delete_playlist removes custom playlists and protects dj-list (by slug, name, and resolved PL ID)."""
    from karaoke import localcache

    mcp_server.add_to_dj_playlist(track_ids=paging_library[:2], playlist_id="temp-test-list")
    deleted = json.loads(mcp_server.delete_playlist("temp-test-list"))
    assert deleted["ok"] is True

    refused = json.loads(mcp_server.delete_playlist("dj-list"))
    assert "error" in refused

    # Seed the primary DJ list with a real-looking PL ID and ensure deleting by PL ID is also refused
    with localcache.connect() as conn:
        localcache.save_playlist(
            playlist_id="PLde7bFUxcmZI",
            name="Karaoke: DJ List",
            search_query="",
            tracks=[],
            url="https://music.youtube.com/playlist?list=PLde7bFUxcmZI",
            source_kind="youtube_music",
            conn=conn,
        )
    monkeypatch.setattr(
        ytmusic_playlist,
        "resolve_or_create_dj_playlist",
        lambda name="Karaoke: DJ List", client=None, conn=None, playlist_key="dj-list": (
            "PLde7bFUxcmZI",
            "https://music.youtube.com/playlist?list=PLde7bFUxcmZI",
        ),
    )
    refused_pl = json.loads(mcp_server.delete_playlist("PLde7bFUxcmZI", delete_remote_ytmusic=True))
    assert "error" in refused_pl
    with localcache.connect() as conn:
        assert localcache.find_saved_playlist_by_id("PLde7bFUxcmZI", conn=conn) is not None


@pytest.fixture
def dj_and_custom_playlists():
    """The live layout that broke 'dj-list': the real DJ list is source_kind
    'youtube_music', and a custom list is source_kind 'dj' and touched later."""
    from karaoke import localcache

    localcache.save_playlist(
        "PLdjlist0001",
        "Karaoke: DJ List",
        tracks=[
            {"track_id": 1, "artist": "Queen", "title": "Don't Stop Me Now", "video_id": "vidqueen001"},
            {"track_id": 2, "artist": "ELO", "title": "Mr. Blue Sky", "video_id": "videlo00002"},
        ],
        url="https://music.youtube.com/playlist?list=PLdjlist0001",
        source_kind="youtube_music",
    )
    time.sleep(0.01)
    localcache.save_playlist(
        "PLfunny00001",
        "Karaoke: Funny Happy",
        tracks=[{"track_id": 3, "artist": "Rick Astley", "title": "Never Gonna Give You Up", "video_id": "dQw4w9WgXcQ"}],
        url="https://music.youtube.com/playlist?list=PLfunny00001",
        source_kind="dj",
    )


def test_dj_list_lookup_ignores_newer_custom_dj_playlist(dj_and_custom_playlists):
    """'dj-list' must resolve to the DJ List by name, not to any source_kind 'dj' playlist."""
    from karaoke import localcache

    pl = localcache.find_saved_playlist_by_id("dj-list")
    assert pl["playlist_id"] == "PLdjlist0001"
    assert pl["name"] == "Karaoke: DJ List"


def test_play_playlist_opens_watch_url_with_list(launcher, dj_and_custom_playlists):
    """play_playlist starts the chosen song inside its playlist so YouTube Music keeps going."""
    calls, _ = launcher
    res = json.loads(mcp_server.play_playlist(playlist_id="PLfunny00001"))
    assert res["status"] == "launched"
    assert res["url"] == "https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=PLfunny00001"
    assert calls[0].arguments["url"] == res["url"]
    assert calls[0].arguments["kind"] == "youtube_music"


def test_play_playlist_start_position(launcher, dj_and_custom_playlists):
    calls, _ = launcher
    res = json.loads(mcp_server.play_playlist(playlist_id="PLdjlist0001", start_position=2))
    assert res["title"] == "Mr. Blue Sky"
    assert res["url"] == "https://music.youtube.com/watch?v=videlo00002&list=PLdjlist0001"


def test_play_playlist_rejects_bad_position(launcher, dj_and_custom_playlists):
    calls, _ = launcher
    res = json.loads(mcp_server.play_playlist(playlist_id="PLdjlist0001", start_position=3))
    assert "error" in res
    assert calls == []


def test_scan_track_complete_track(playable_track, monkeypatch):
    """A fully analysed track returns status 'complete' without doing any work."""
    from karaoke import postprocess_queue, search
    monkeypatch.setattr(postprocess_queue, "needs_postprocessing", lambda tid, conn: [])
    monkeypatch.setattr(search, "clap_vector_for", lambda tid: [0.1] * 512)

    res = json.loads(mcp_server.scan_track(track_id=playable_track))
    assert res["status"] == "complete"
    assert res["missing_before"] == []
    assert res["track_id"] == playable_track
    assert res["source"] is not None
    assert res["steps_done"] == []


def test_scan_track_finds_source_and_queues(monkeypatch):
    """When a track has no source, scan_track finds one and queues postprocessing."""
    from karaoke import find_sources, localcache, postprocess_queue, search

    tid = 8801
    with localcache.connect() as conn:
        conn.execute(
            "INSERT INTO tracks (track_id, artist, title, album, duration, play_count) VALUES (%s, %s, %s, %s, %s, %s)",
            (tid, "Artist X", "Song Y", "Album Z", 180.0, 0),
        )

    monkeypatch.setattr(postprocess_queue, "needs_postprocessing", lambda t, c: ["analysis", "chords"])
    monkeypatch.setattr(search, "clap_vector_for", lambda t: None)
    monkeypatch.setattr(find_sources, "resolve_and_save_source", lambda t, c: "https://www.youtube.com/watch?v=mockFound123")

    published_tasks = []

    def mock_publish(artist, title, url="", full=False, **kw):
        published_tasks.append({"artist": artist, "title": title, "url": url, "full": full})
        return True

    monkeypatch.setattr(postprocess_queue, "publish_postprocess_task", mock_publish)

    res = json.loads(mcp_server.scan_track(track_id=tid, mode="auto"))
    assert res["status"] == "queued"
    assert res["mode_used"] == "queue"
    assert "analysis" in res["missing_before"]
    assert "clap" in res["missing_before"]
    assert res["source"]["url"] == "https://www.youtube.com/watch?v=mockFound123"
    assert res["source"]["found_now"] is True
    assert len(published_tasks) == 1
    assert published_tasks[0]["full"] is True
    assert published_tasks[0]["url"] == "https://www.youtube.com/watch?v=mockFound123"


def test_scan_track_dry_run_never_writes(monkeypatch):
    """dry_run=True must report candidate source and missing steps without writing to DB or broker."""
    from karaoke import find_sources, localcache, postprocess_queue, search

    tid = 8802
    with localcache.connect() as conn:
        conn.execute(
            "INSERT INTO tracks (track_id, artist, title, album, duration, play_count) VALUES (%s, %s, %s, %s, %s, %s)",
            (tid, "Artist Dry", "Song Run", "Album", 200.0, 0),
        )

    monkeypatch.setattr(postprocess_queue, "needs_postprocessing", lambda t, c: ["analysis"])
    monkeypatch.setattr(search, "clap_vector_for", lambda t: None)
    monkeypatch.setattr(find_sources, "find_for", lambda cand: {"url": "https://www.youtube.com/watch?v=mockCandidate999"})

    add_source_calls = []
    publish_calls = []
    monkeypatch.setattr(localcache, "add_track_source", lambda *a, **k: add_source_calls.append((a, k)))
    monkeypatch.setattr(postprocess_queue, "publish_postprocess_task", lambda *a, **k: publish_calls.append((a, k)))

    res = json.loads(mcp_server.scan_track(track_id=tid, mode="queue", dry_run=True))
    assert res["status"] == "queued"
    assert res["source"]["url"] == "https://www.youtube.com/watch?v=mockCandidate999"
    assert res["source"]["found_now"] is True
    assert "analysis" in res["missing_before"]
    assert len(add_source_calls) == 0
    assert len(publish_calls) == 0


def test_scan_track_sample_mode_rejects_different_playing_track(monkeypatch):
    """mode='sample' refuses to record if the track is not the one currently playing."""
    from karaoke import localcache, postprocess_queue, sample_audio, search

    tid = 8803
    with localcache.connect() as conn:
        conn.execute(
            "INSERT INTO tracks (track_id, artist, title, album, duration, play_count) VALUES (%s, %s, %s, %s, %s, %s)",
            (tid, "Some Band", "Different Song", "Album", 220.0, 0),
        )

    monkeypatch.setattr(postprocess_queue, "needs_postprocessing", lambda t, c: ["analysis"])
    monkeypatch.setattr(search, "clap_vector_for", lambda t: None)
    monkeypatch.setattr(mcp_server, "get_now_playing", lambda: json.dumps({"status": "playing", "track_id": 9999}))

    sample_calls = []
    monkeypatch.setattr(sample_audio, "sample_and_analyse", lambda *a, **k: sample_calls.append((a, k)))

    res = json.loads(mcp_server.scan_track(track_id=tid, mode="sample"))
    assert res["status"] == "error"
    assert "currently playing" in res["note"]
    assert len(sample_calls) == 0


def test_scan_track_no_source_found_and_not_playing(monkeypatch):
    """When no source is found and track is not playing, status is 'no_source'."""
    from karaoke import find_sources, localcache, postprocess_queue, search

    tid = 8804
    with localcache.connect() as conn:
        conn.execute(
            "INSERT INTO tracks (track_id, artist, title, album, duration, play_count) VALUES (%s, %s, %s, %s, %s, %s)",
            (tid, "Obscure Artist", "Rare Song", "Album", 150.0, 0),
        )

    monkeypatch.setattr(postprocess_queue, "needs_postprocessing", lambda t, c: ["analysis", "chords"])
    monkeypatch.setattr(search, "clap_vector_for", lambda t: None)
    monkeypatch.setattr(find_sources, "resolve_and_save_source", lambda t, c: None)
    monkeypatch.setattr(mcp_server, "get_now_playing", lambda: json.dumps({"status": "idle"}))

    res = json.loads(mcp_server.scan_track(track_id=tid, mode="auto"))
    assert res["status"] == "no_source"
    assert res["source"] is None


def test_suggest_next_tracks_missing_clap_returns_note(playable_track, monkeypatch):
    """When the seed has no CLAP vector, acoustic suggestion returns a note pointing to scan_track."""
    from karaoke import dj_suggest, queue_suggest
    monkeypatch.setattr(dj_suggest, "suggest", lambda *a, **k: [])
    monkeypatch.setattr(queue_suggest, "suggest_for_track", lambda *a, **k: [])

    res = json.loads(mcp_server.suggest_next_tracks(track_id=playable_track, strategy="acoustic"))
    assert res["strategy"] == "acoustic_vector"
    assert res["suggestions"] == []
    assert "note" in res
    assert "scan_track" in res["note"]


def test_suggest_next_tracks_flow_fallback_returns_note(playable_track, monkeypatch):
    """When 'flow' cannot find ranked suggestions, it falls back to harmonic and adds a note."""
    from karaoke import dj_suggest
    monkeypatch.setattr(dj_suggest, "suggest", lambda *a, **k: [])

    res = json.loads(mcp_server.suggest_next_tracks(track_id=playable_track, strategy="flow"))
    assert res["strategy"] == "harmonic"
    assert "note" in res
    assert "scan_track" in res["note"]

