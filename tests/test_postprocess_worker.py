"""Tests for the post-processing worker's task handling and ACK semantics."""
from unittest.mock import MagicMock, patch

import pytest

from karaoke import postprocess_worker as w


def _conn_with(track_id=1, pending=("analysis",), url="https://youtu.be/abc"):
    """Patch the DB-facing helpers so process_task runs against fakes."""
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.execute.return_value.fetchone.return_value = (url,) if url else None
    return conn


def test_process_task_raises_when_analysis_has_no_url():
    """No watchable URL means the work is undone — it must not silently pass."""
    conn = _conn_with(url=None)
    with patch("karaoke.postprocess_worker.localcache.connect", return_value=conn), \
         patch("karaoke.postprocess_worker.localcache.find_track_id", return_value=1), \
         patch("karaoke.postprocess_worker.localcache.extract_youtube_id", return_value=None), \
         patch("karaoke.postprocess_worker.needs_postprocessing", return_value=["analysis"]):
        with pytest.raises(RuntimeError, match="analysis"):
            w.process_task({"artist": "A", "title": "B", "url": ""})


def test_process_task_raises_when_analysis_fails():
    conn = _conn_with()
    with patch("karaoke.postprocess_worker.localcache.connect", return_value=conn), \
         patch("karaoke.postprocess_worker.localcache.find_track_id", return_value=1), \
         patch("karaoke.postprocess_worker.localcache.extract_youtube_id", return_value="abc"), \
         patch("karaoke.postprocess_worker.needs_postprocessing", return_value=["analysis"]), \
         patch("karaoke.postprocess_worker._ensure_download", return_value="/tmp/a.webm"), \
         patch("karaoke.postprocess_worker._run_analysis", return_value=False):
        with pytest.raises(RuntimeError, match="analysis"):
            w.process_task({"artist": "A", "title": "B", "url": "https://youtu.be/abc"})


def test_process_task_succeeds_quietly_when_work_completes():
    conn = _conn_with()
    with patch("karaoke.postprocess_worker.localcache.connect", return_value=conn), \
         patch("karaoke.postprocess_worker.localcache.find_track_id", return_value=1), \
         patch("karaoke.postprocess_worker.localcache.extract_youtube_id", return_value="abc"), \
         patch("karaoke.postprocess_worker.needs_postprocessing", return_value=["analysis"]), \
         patch("karaoke.postprocess_worker._ensure_download", return_value="/tmp/a.webm"), \
         patch("karaoke.postprocess_worker._run_analysis", return_value=True):
        w.process_task({"artist": "A", "title": "B", "url": "https://youtu.be/abc"})


def test_missing_captions_is_terminal_not_a_failure():
    """"no-captions" cannot be fixed by retrying, so it must not requeue."""
    conn = _conn_with()
    with patch("karaoke.postprocess_worker.localcache.connect", return_value=conn), \
         patch("karaoke.postprocess_worker.localcache.find_track_id", return_value=1), \
         patch("karaoke.postprocess_worker.localcache.extract_youtube_id", return_value="abc"), \
         patch("karaoke.postprocess_worker.needs_postprocessing", return_value=["timings"]), \
         patch("karaoke.postprocess_worker._run_timings", return_value="no-captions"):
        w.process_task({"artist": "A", "title": "B", "url": "https://youtu.be/abc"})


def test_timing_error_is_retryable():
    conn = _conn_with()
    with patch("karaoke.postprocess_worker.localcache.connect", return_value=conn), \
         patch("karaoke.postprocess_worker.localcache.find_track_id", return_value=1), \
         patch("karaoke.postprocess_worker.localcache.extract_youtube_id", return_value="abc"), \
         patch("karaoke.postprocess_worker.needs_postprocessing", return_value=["timings"]), \
         patch("karaoke.postprocess_worker._run_timings", return_value="error"):
        with pytest.raises(RuntimeError, match="timings"):
            w.process_task({"artist": "A", "title": "B", "url": "https://youtu.be/abc"})


def _delivery(redelivered=False):
    m = MagicMock()
    m.delivery_tag = 42
    m.redelivered = redelivered
    return m


def test_handle_message_acks_on_success():
    ch, method = MagicMock(), _delivery()
    with patch("karaoke.postprocess_worker.process_task"):
        w.handle_message(ch, method, b'{"artist":"A","title":"B"}')
    ch.basic_ack.assert_called_once_with(delivery_tag=42)
    ch.basic_nack.assert_not_called()


def test_handle_message_requeues_first_failure():
    """The old code ACKed in a finally block, silently dropping failed work."""
    ch, method = MagicMock(), _delivery(redelivered=False)
    with patch("karaoke.postprocess_worker.process_task", side_effect=RuntimeError):
        w.handle_message(ch, method, b'{"artist":"A","title":"B"}')
    ch.basic_ack.assert_not_called()
    ch.basic_nack.assert_called_once_with(delivery_tag=42, requeue=True)


def test_handle_message_drops_poison_task_on_second_failure():
    ch, method = MagicMock(), _delivery(redelivered=True)
    with patch("karaoke.postprocess_worker.process_task", side_effect=RuntimeError):
        w.handle_message(ch, method, b'{"artist":"A","title":"B"}')
    ch.basic_nack.assert_called_once_with(delivery_tag=42, requeue=False)


def test_handle_message_drops_malformed_body():
    ch, method = MagicMock(), _delivery()
    w.handle_message(ch, method, b'not json')
    ch.basic_ack.assert_called_once_with(delivery_tag=42)
