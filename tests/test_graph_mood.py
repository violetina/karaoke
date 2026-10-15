"""Mood timelines on the similarity graph."""
from contextlib import nullcontext
from unittest.mock import MagicMock

from karaoke import graph_view


def _conn_with(rows):
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = rows
    return conn


def test_a_node_gets_its_arc_swings_and_segments(monkeypatch):
    row = {"track_id": 5103, "duration_s": 269.0,
           "segments": [{"start_s": 0.0, "end_s": 40.0, "mood": "sad", "alt": None},
                        {"start_s": 40.0, "end_s": 269.0, "mood": "angry", "alt": "sad"}],
           "summary": {"dominant": "angry", "arc": "sad → angry", "swing_count": 1,
                       "borderline_share": 0.2}}
    monkeypatch.setattr("karaoke.localcache.connect", lambda *a, **k: nullcontext(_conn_with([row])))
    monkeypatch.setattr("karaoke.mood_timeline.ensure_schema", lambda conn: None)
    nodes = {5103: {"dominant_mood": "sad", "color": "#000"}, 7: {"dominant_mood": "happy"}}
    graph_view._add_mood_timelines(nodes, "mood")
    n = nodes[5103]
    assert n["dominant_mood"] == "angry"            # the timeline wins over lyrics alone
    assert n["color"] == graph_view.MOOD_COLORS["angry"]
    assert n["mood_arc"] == "sad → angry" and n["mood_swings"] == 1
    assert n["mood_segments"] == [[0.0, 40.0, "sad", None], [40.0, 269.0, "angry", "sad"]]
    assert nodes[7] == {"dominant_mood": "happy"}    # no timeline: untouched


def test_other_strategies_keep_their_colour(monkeypatch):
    row = {"track_id": 1, "duration_s": 60.0, "segments": [],
           "summary": {"dominant": "sad", "arc": "sad", "swing_count": 0, "borderline_share": 0}}
    monkeypatch.setattr("karaoke.localcache.connect", lambda *a, **k: nullcontext(_conn_with([row])))
    monkeypatch.setattr("karaoke.mood_timeline.ensure_schema", lambda conn: None)
    nodes = {1: {"color": "#abc"}}
    graph_view._add_mood_timelines(nodes, "structural")
    assert nodes[1]["color"] == "#abc"


def test_the_page_carries_the_mood_colours_and_strip():
    page = graph_view.GRAPH_HTML
    assert "__MOOD_COLORS__" not in page
    assert '"angry": "#ef4444"' in page
    assert 'id="d-moodstrip"' in page and "function renderMoodArc" in page
