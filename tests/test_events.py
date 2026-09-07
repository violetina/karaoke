"""Tests for the karaoke-events ledger reader (Argo Events -> OpenSearch)."""
from __future__ import annotations

from unittest.mock import MagicMock

from karaoke import events


def _fake_client(hits, exists=True):
    c = MagicMock()
    c.indices.exists.return_value = exists
    c.search.return_value = {"hits": {"hits": hits}}
    return c


def test_recent_events_maps_hits(monkeypatch):
    hits = [
        {"_source": {"task_id": "abc", "task_name": "karaoke.tasks.postprocess_track",
                     "state": "SUCCESS", "ts": "1788800803"}},
    ]
    monkeypatch.setattr("karaoke.osclient.client", lambda: _fake_client(hits))
    out = events.recent_events(limit=5)
    assert out == [{"task_id": "abc", "task_name": "karaoke.tasks.postprocess_track",
                    "state": "SUCCESS", "ts": 1788800803.0}]


def test_recent_events_since_ts_builds_range_query(monkeypatch):
    captured = {}

    def _search(index, body):
        captured["body"] = body
        return {"hits": {"hits": []}}

    c = MagicMock()
    c.indices.exists.return_value = True
    c.search.side_effect = _search
    monkeypatch.setattr("karaoke.osclient.client", lambda: c)

    events.recent_events(since_ts=100.0, limit=10)
    assert captured["body"]["query"] == {"range": {"ts": {"gt": 100.0}}}
    assert captured["body"]["size"] == 10


def test_recent_events_missing_index_is_empty(monkeypatch):
    monkeypatch.setattr("karaoke.osclient.client", lambda: _fake_client([], exists=False))
    assert events.recent_events() == []


def test_recent_events_unreachable_cluster_is_empty(monkeypatch):
    def _boom():
        raise ConnectionError("no cluster")
    monkeypatch.setattr("karaoke.osclient.client", _boom)
    assert events.recent_events() == []
