"""Celery orchestration tests for post-processing."""
from __future__ import annotations

from unittest.mock import patch
from typing import Any, cast

from karaoke import postprocess_queue as q
from karaoke import tasks


def test_celery_task_wraps_existing_process_task():
    payload = {"artist": "A", "title": "B", "url": "https://youtu.be/x"}
    with patch("karaoke.postprocess_worker.process_task") as process:
        result = cast(Any, tasks.postprocess_track).run(payload)
    process.assert_called_once_with(payload)
    assert result == {"status": "ok", "artist": "A", "title": "B"}


def test_publish_uses_celery_by_default(monkeypatch):
    published = []
    monkeypatch.delenv("KARAOKE_ORCHESTRATOR", raising=False)
    monkeypatch.setattr(tasks, "enqueue_postprocess",
                        lambda payload: published.append(payload) or "task-1")

    assert q.publish_postprocess_task(" A ", " B ", " https://youtu.be/x ") is True
    assert published == [{
        "artist": "A",
        "title": "B",
        "url": "https://youtu.be/x",
    }]


def test_publish_can_use_legacy_pika_path(monkeypatch):
    monkeypatch.setenv("KARAOKE_ORCHESTRATOR", "legacy")
    calls = []

    class Props:
        def __init__(self, delivery_mode):
            self.delivery_mode = delivery_mode

    class Channel:
        def queue_declare(self, **kw):
            calls.append(("queue", kw))
        def basic_publish(self, **kw):
            calls.append(("publish", kw))

    class Conn:
        def channel(self):
            return Channel()
        def close(self):
            calls.append(("close", {}))

    monkeypatch.setattr(q.pika, "PlainCredentials", lambda u, p: (u, p))
    monkeypatch.setattr(q.pika, "ConnectionParameters", lambda **kw: kw)
    monkeypatch.setattr(q.pika, "BasicProperties", Props)
    monkeypatch.setattr(q.pika, "BlockingConnection", lambda params: Conn())

    assert q.publish_postprocess_task("A", "B", "") is True
    assert any(name == "publish" for name, _ in calls)


def test_empty_publish_is_refused():
    assert q.publish_postprocess_task("", "", "") is False
