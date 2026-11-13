"""Tests for the AI DJ 'ingest on miss' branch in dashboard_api.

Covers: detecting a vibe question about the currently-playing-but-unanalyzed
track, and the ack -> ingest_started -> (answer|timeout) SSE event sequence,
without touching real Ollama/Celery/RabbitMQ.
"""
from __future__ import annotations

import json

import pytest

from karaoke import dashboard_api as api


def test_detect_unanalyzed_now_playing_literal_chip_click():
    """The reported bug: the quick-chip interpolates currentTitle verbatim,
    so asked_title == now_title exactly. This must trigger ingestion when
    query_facts came back empty."""
    target = api._detect_unanalyzed_now_playing(
        "What's the vibe of Feuksley Ma'am, the Hearing?",
        now_artist="Feuksley",
        now_title="Feuksley Ma'am, the Hearing",
        query_facts="",
    )
    assert target == {"artist": "Feuksley", "title": "Feuksley Ma'am, the Hearing"}


def test_detect_unanalyzed_now_playing_none_when_track_already_in_library():
    """If the specific (artist, title) pair already resolves in our library,
    this is NOT the ingest case -- even if generic OpenSearch facts happen
    to be empty for some other reason."""
    from karaoke import localcache

    with localcache.connect() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO tracks (artist, title, album, duration, play_count) VALUES (%s, %s, '', 200, 0)",
            ("Queen", "Bohemian Rhapsody"),
        )

    target = api._detect_unanalyzed_now_playing(
        "What's the vibe of Bohemian Rhapsody?",
        now_artist="Queen",
        now_title="Bohemian Rhapsody",
        query_facts="",
    )
    assert target is None


def test_detect_unanalyzed_now_playing_none_when_nothing_playing():
    """A vibe question with no current track playing must not try to ingest."""
    target = api._detect_unanalyzed_now_playing(
        "What's the vibe of some random song?",
        now_artist="",
        now_title="",
        query_facts="",
    )
    assert target is None


def test_detect_unanalyzed_now_playing_none_for_unrelated_question():
    """Asking about a different artist/song than what's playing should not
    trigger ingestion of the currently playing track."""
    target = api._detect_unanalyzed_now_playing(
        "Tell me about Depeche Mode side projects",
        now_artist="Queen",
        now_title="Bohemian Rhapsody",
        query_facts="",
    )
    assert target is None


@pytest.mark.anyio
async def test_ingest_then_answer_stream_emits_ack_then_ingest_started(monkeypatch):
    """Verify the SSE event sequence without touching real Ollama/Celery."""
    from karaoke import postprocess_queue

    published = {}

    def fake_publish(artist, title, url="", include_timings=False, full=False, steps=None):
        published["artist"] = artist
        published["title"] = title
        published["full"] = full
        return True

    monkeypatch.setattr(postprocess_queue, "publish_postprocess_task", fake_publish)

    # Force the polling loop to time out immediately (facts never appear).
    monkeypatch.setattr(api, "_retrieve_query_facts", lambda *a, **k: "")

    events = []
    async for ev in api._ingest_then_answer_stream(
        {"artist": "Feuksley", "title": "Feuksley Ma'am, the Hearing"},
        "What's the vibe of Feuksley Ma'am, the Hearing?",
        "Feuksley", "Feuksley Ma'am, the Hearing", "",
        "ollama", None,
        max_wait_s=0.01, poll_interval_s=0.01,
    ):
        events.append(ev)

    event_names = [e["event"] for e in events]
    assert event_names[0] == "token"
    first_payload = json.loads(events[0]["data"])
    assert "analyze" in first_payload["token"].lower() or "listen" in first_payload["token"].lower()

    assert "ingest_started" in event_names
    ingest_payload = json.loads(events[event_names.index("ingest_started")]["data"])
    assert ingest_payload == {"artist": "Feuksley", "title": "Feuksley Ma'am, the Hearing"}

    assert published["artist"] == "Feuksley"
    assert published["full"] is True

    assert event_names[-1] == "done"
    # Timeout path: one of the token events should mention "still working".
    joined_tokens = " ".join(
        json.loads(e["data"]).get("token", "") for e in events if e["event"] == "token"
    )
    assert "still working" in joined_tokens.lower()


@pytest.mark.anyio
async def test_ingest_then_answer_stream_streams_real_answer_once_facts_land(monkeypatch):
    """When facts show up on the first poll, stream the real grounded answer
    through the chosen provider instead of timing out."""
    from karaoke import postprocess_queue, ai_providers

    monkeypatch.setattr(postprocess_queue, "publish_postprocess_task", lambda *a, **k: True)
    monkeypatch.setattr(api, "_retrieve_query_facts", lambda *a, **k: "Key: C major, BPM: 120")

    async def fake_stream_chat(provider_id, model, system_prompt, user_msg, timeout=60.0):
        assert "C major" in system_prompt
        for tok in ["It's ", "upbeat ", "and bright."]:
            yield tok

    monkeypatch.setattr(ai_providers, "stream_chat", fake_stream_chat)

    events = []
    async for ev in api._ingest_then_answer_stream(
        {"artist": "Feuksley", "title": "Feuksley Ma'am, the Hearing"},
        "What's the vibe of Feuksley Ma'am, the Hearing?",
        "Feuksley", "Feuksley Ma'am, the Hearing", "",
        "ollama", None,
        max_wait_s=1.0, poll_interval_s=0.01,
    ):
        events.append(ev)

    event_names = [e["event"] for e in events]
    assert event_names[-1] == "done"
    joined_tokens = " ".join(
        json.loads(e["data"]).get("token", "") for e in events if e["event"] == "token"
    )
    assert "upbeat" in joined_tokens


@pytest.mark.anyio
async def test_ingest_then_answer_stream_emits_error_event_on_publish_failure(monkeypatch):
    from karaoke import postprocess_queue

    def boom(*a, **k):
        raise RuntimeError("broker down")

    monkeypatch.setattr(postprocess_queue, "publish_postprocess_task", boom)

    events = []
    async for ev in api._ingest_then_answer_stream(
        {"artist": "Feuksley", "title": "Feuksley Ma'am, the Hearing"},
        "What's the vibe of Feuksley Ma'am, the Hearing?",
        "Feuksley", "Feuksley Ma'am, the Hearing", "",
        "ollama", None,
    ):
        events.append(ev)

    assert events[-1]["event"] == "error"
    assert "broker down" in json.loads(events[-1]["data"])["error"]
