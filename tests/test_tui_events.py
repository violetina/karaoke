"""Unit tests for TUI platform event polling and reactive refresh."""
from __future__ import annotations

from unittest.mock import MagicMock
from karaoke.tui import KaraokeTui


def test_tui_initializes_event_timestamp():
    app = KaraokeTui.__new__(KaraokeTui)
    app._last_event_ts = 12345.0
    assert app._last_event_ts == 12345.0


def test_tui_handles_postprocess_event(monkeypatch):
    app = KaraokeTui.__new__(KaraokeTui)
    app._sync_key = ("some", "song")
    app._det = MagicMock(is_active=False)

    poll_detection_called = []
    load_songs_called = []
    notifications = []

    monkeypatch.setattr(app, "_poll_detection", lambda: poll_detection_called.append(True))
    monkeypatch.setattr(app, "load_songs", lambda: load_songs_called.append(True))
    monkeypatch.setattr(app, "notify", lambda msg, **k: notifications.append(msg))

    events = [
        {"task_name": "karaoke.tasks.postprocess_track", "state": "SUCCESS", "ts": 100.0}
    ]
    app._handle_platform_events(events)

    assert app._sync_key is None
    assert len(poll_detection_called) == 1
    assert len(load_songs_called) == 1
    assert any("Track data updated" in n for n in notifications)


def test_tui_handles_queue_and_metadata_events(monkeypatch):
    app = KaraokeTui.__new__(KaraokeTui)
    app._sync_key = ("some", "song")
    app._det = MagicMock(is_active=True)

    queue_rendered = []
    poll_detection_called = []
    monkeypatch.setattr(app, "_render_queue", lambda: queue_rendered.append(True))
    monkeypatch.setattr(app, "_poll_detection", lambda: poll_detection_called.append(True))
    monkeypatch.setattr(app, "notify", lambda msg, **k: None)

    events = [
        {"task_name": "karaoke.playback.queue_advance", "state": "SUCCESS", "ts": 101.0},
        {"task_name": "karaoke.metadata.wikibase_link", "state": "SUCCESS", "ts": 102.0},
    ]
    app._handle_platform_events(events)

    assert len(queue_rendered) == 1
    assert len(poll_detection_called) == 1
