"""Celery orchestration tests for post-processing."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch, ANY
from typing import Any, cast
from pathlib import Path

from karaoke import postprocess_queue as q
from karaoke import celery_app
from karaoke import tasks
from karaoke.tasks import PostprocessContext # Import the new dataclass
from karaoke import localcache # For direct access in tests
from karaoke import postprocess_worker # For direct access in tests


# --- Test for resolve_track_id task ---
def test_resolve_track_id_found(monkeypatch):
    mock_find_track_id = MagicMock(return_value=123)
    mock_find_track_by_url = MagicMock(return_value=None)
    mock_extract_youtube_id = MagicMock(return_value="video_id")
    mock_needs_postprocessing = MagicMock(return_value=["analysis", "sync"])

    monkeypatch.setattr(localcache, "find_track_id", mock_find_track_id)
    monkeypatch.setattr(localcache, "find_track_by_url", mock_find_track_by_url)
    monkeypatch.setattr(localcache, "extract_youtube_id", mock_extract_youtube_id)
    monkeypatch.setattr(q, "needs_postprocessing", mock_needs_postprocessing)
    monkeypatch.setattr(localcache, "connect", MagicMock())

    payload = {"artist": "Test Artist", "title": "Test Title", "url": "http://youtube.com/v1"}
    result_dict = tasks.resolve_track_id.run(payload)

    context = PostprocessContext.from_dict(result_dict)
    assert context.track_id == 123
    assert context.url == "http://youtube.com/v1"
    assert context.pending == ["analysis", "sync"]
    mock_find_track_id.assert_called_once()
    mock_extract_youtube_id.assert_called_once()
    mock_needs_postprocessing.assert_called_once()

def test_resolve_track_id_not_found():
    mock_find_track_id = MagicMock(return_value=None)
    mock_find_track_by_url = MagicMock(return_value=None)

    with patch.object(localcache, "find_track_id", new=mock_find_track_id):
        with patch.object(localcache, "find_track_by_url", new=mock_find_track_by_url):
            with patch.object(localcache, "connect", new=MagicMock()):
                payload = {"artist": "Unknown", "title": "Unknown", "url": ""}
                try:
                    tasks.resolve_track_id.run(payload)
                    assert False, "ValueError was not raised"
                except ValueError as e:
                    assert "Track not found" in str(e)
                mock_find_track_id.assert_called_once()

# --- Test for download_audio task ---
def test_download_audio_no_pending(tmp_path):
    context = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=tmp_path / "audio.mp3", pending=[]
    )
    result = tasks.download_audio.run(context.to_dict())
    assert result == context.to_dict() # Should return unchanged context

def test_download_audio_success(monkeypatch, tmp_path):
    mock_run_download_logic = MagicMock(return_value=tmp_path / "downloaded.mp3")
    monkeypatch.setattr(postprocess_worker, "run_download_logic", mock_run_download_logic)

    context = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        pending=["analysis"]
    )
    result_dict = tasks.download_audio.run(context.to_dict())
    result_context = PostprocessContext.from_dict(result_dict)

    assert result_context.audio_path == tmp_path / "downloaded.mp3"
    mock_run_download_logic.assert_called_once_with(context.url, context.cookies_from_browser)

def test_download_audio_failure(monkeypatch):
    mock_run_download_logic = MagicMock(return_value=None)
    monkeypatch.setattr(postprocess_worker, "run_download_logic", mock_run_download_logic)

    context = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        pending=["analysis", "sync"]
    )
    result_dict = tasks.download_audio.run(context.to_dict())
    result_context = PostprocessContext.from_dict(result_dict)

    assert "analysis" not in result_context.pending
    assert "sync" not in result_context.pending
    assert result_context.audio_path is None
    mock_run_download_logic.assert_called_once()

# --- Test for analyze_audio task ---
def test_analyze_audio_no_pending_or_no_audio(tmp_path):
    context = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=None, pending=[]
    )
    result = tasks.analyze_audio.run(context.to_dict())
    assert result == context.to_dict()

    context_no_audio = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=None, pending=["analysis"]
    )
    result_no_audio = tasks.analyze_audio.run(context_no_audio.to_dict())
    assert result_no_audio == context_no_audio.to_dict()

def test_analyze_audio_success(monkeypatch, tmp_path):
    mock_run_analysis_logic = MagicMock(return_value=True)
    monkeypatch.setattr(postprocess_worker, "run_analysis_logic", mock_run_analysis_logic)
    monkeypatch.setattr(localcache, "connect", MagicMock()) # Fix: direct import

    context = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=tmp_path / "audio.mp3", pending=["analysis"]
    )
    result = tasks.analyze_audio.run(context.to_dict())

    assert result == context.to_dict()
    mock_run_analysis_logic.assert_called_once_with(context.track_id, context.audio_path, ANY)

def test_analyze_audio_failure(monkeypatch, tmp_path):
    mock_run_analysis_logic = MagicMock(return_value=False)
    monkeypatch.setattr(postprocess_worker, "run_analysis_logic", mock_run_analysis_logic)
    monkeypatch.setattr(localcache, "connect", MagicMock()) # Fix: direct import

    context = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=tmp_path / "audio.mp3", pending=["analysis"]
    )
    result = tasks.analyze_audio.run(context.to_dict())

    assert result == context.to_dict()
    mock_run_analysis_logic.assert_called_once()

# --- Test for upgrade_timings task ---
def test_upgrade_timings_no_pending(tmp_path):
    context = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=tmp_path / "audio.mp3", pending=[]
    )
    result = tasks.upgrade_timings.run(context.to_dict())
    assert result == context.to_dict()

def test_upgrade_timings_success(monkeypatch, tmp_path):
    mock_run_timings_logic = MagicMock(return_value="upgraded")
    monkeypatch.setattr(postprocess_worker, "run_timings_logic", mock_run_timings_logic)
    monkeypatch.setattr(localcache, "connect", MagicMock()) # Fix: direct import

    context = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=tmp_path / "audio.mp3", pending=["timings"]
    )
    result = tasks.upgrade_timings.run(context.to_dict())

    assert result == context.to_dict()
    mock_run_timings_logic.assert_called_once_with(context.track_id, ANY, context.cookies_from_browser)

def test_upgrade_timings_failure(monkeypatch, tmp_path):
    mock_run_timings_logic = MagicMock(return_value="error")
    monkeypatch.setattr(postprocess_worker, "run_timings_logic", mock_run_timings_logic)
    monkeypatch.setattr(localcache, "connect", MagicMock()) # Fix: direct import

    context = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=tmp_path / "audio.mp3", pending=["timings"]
    )
    try:
        tasks.upgrade_timings.run(context.to_dict())
        assert False, "RuntimeError was not raised"
    except RuntimeError as e:
        assert "Timings upgrade failed" in str(e)
    mock_run_timings_logic.assert_called_once()

# --- Test for sync_lyrics task ---
def test_sync_lyrics_no_pending_or_no_audio(tmp_path):
    context = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=None, pending=[]
    )
    result = tasks.sync_lyrics.run(context.to_dict())
    assert result == context.to_dict()

    context_no_audio = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=None, pending=["sync"]
    )
    result_no_audio = tasks.sync_lyrics.run(context_no_audio.to_dict())
    assert result_no_audio == context_no_audio.to_dict()

def test_sync_lyrics_success(monkeypatch, tmp_path):
    mock_run_sync_logic = MagicMock(return_value=True)
    monkeypatch.setattr(postprocess_worker, "run_sync_logic", mock_run_sync_logic)
    monkeypatch.setattr(localcache, "connect", MagicMock()) # Fix: direct import

    context = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=tmp_path / "audio.mp3", pending=["sync"]
    )
    result = tasks.sync_lyrics.run(context.to_dict())

    assert result == context.to_dict()
    mock_run_sync_logic.assert_called_once_with(context.track_id, context.audio_path, ANY)

def test_sync_lyrics_failure(monkeypatch, tmp_path):
    mock_run_sync_logic = MagicMock(return_value=False)
    monkeypatch.setattr(postprocess_worker, "run_sync_logic", mock_run_sync_logic)
    monkeypatch.setattr(localcache, "connect", MagicMock()) # Fix: direct import

    context = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=tmp_path / "audio.mp3", pending=["sync"]
    )
    try:
        tasks.sync_lyrics.run(context.to_dict())
        assert False, "RuntimeError was not raised"
    except RuntimeError as e:
        assert "Lyrics sync failed" in str(e)
    mock_run_sync_logic.assert_called_once()

# --- Test for rebuild_vectors task ---
def test_rebuild_vectors_no_pending(tmp_path):
    context = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=tmp_path / "audio.mp3", pending=[]
    )
    result = tasks.rebuild_vectors.run(context.to_dict())
    assert result == context.to_dict()

def test_rebuild_vectors_success(monkeypatch, tmp_path):
    mock_run_vectors_logic = MagicMock(return_value=True)
    monkeypatch.setattr(postprocess_worker, "run_vectors_logic", mock_run_vectors_logic)
    monkeypatch.setattr(localcache, "connect", MagicMock()) # Fix: direct import

    context = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=tmp_path / "audio.mp3", pending=["vectors"]
    )
    result = tasks.rebuild_vectors.run(context.to_dict())

    assert result == context.to_dict()
    mock_run_vectors_logic.assert_called_once_with(context.track_id, ANY)

def test_rebuild_vectors_failure(monkeypatch, tmp_path):
    mock_run_vectors_logic = MagicMock(return_value=False)
    monkeypatch.setattr(postprocess_worker, "run_vectors_logic", mock_run_vectors_logic)
    monkeypatch.setattr(localcache, "connect", MagicMock()) # Fix: direct import

    context = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=tmp_path / "audio.mp3", pending=["vectors"]
    )
    try:
        tasks.rebuild_vectors.run(context.to_dict())
        assert False, "RuntimeError was not raised"
    except RuntimeError as e:
        assert "Vectors rebuild failed" in str(e)
    mock_run_vectors_logic.assert_called_once()


# --- Test for postprocess_track orchestrator task ---
def test_postprocess_track_orchestrates_flow(monkeypatch):
    mock_resolve_track_id_result = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        pending=["download", "analysis", "timings", "sync", "vectors"]
    ).to_dict()
    mock_download_audio_result = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=Path("/tmp/audio.mp3"),
        pending=["analysis", "timings", "sync", "vectors"]
    ).to_dict()
    mock_analyze_audio_result = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=Path("/tmp/audio.mp3"),
        pending=["timings", "sync", "vectors"]
    ).to_dict()
    mock_upgrade_timings_result = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=Path("/tmp/audio.mp3"),
        pending=["sync", "vectors"]
    ).to_dict()
    mock_sync_lyrics_result = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=Path("/tmp/audio.mp3"),
        pending=["vectors"]
    ).to_dict()
    mock_rebuild_vectors_result = PostprocessContext(
        artist="A", title="B", url="http://youtube.com/vid1", track_id=1,
        audio_path=Path("/tmp/audio.mp3"),
        pending=[]
    ).to_dict()

    # Mock the tasks themselves, and their .s() method which returns a signature
    mock_resolve_task = MagicMock()
    mock_download_task = MagicMock()
    mock_analyze_task = MagicMock()
    mock_harmony_task = MagicMock()
    mock_upgrade_task = MagicMock()
    mock_sync_task = MagicMock()
    mock_rebuild_task = MagicMock()

    # The .s() method returns a mock, and on that mock, .set() returns the final result
    mock_resolve_task_s_return_value = MagicMock()
    mock_resolve_task_s_return_value.set.return_value = mock_resolve_track_id_result
    mock_resolve_task.s.return_value = mock_resolve_task_s_return_value

    mock_download_task.s.return_value = mock_download_audio_result
    mock_analyze_task.s.return_value = mock_analyze_audio_result
    mock_harmony_task.s.return_value = mock_analyze_audio_result
    mock_upgrade_task.s.return_value = mock_upgrade_timings_result
    mock_sync_task.s.return_value = mock_sync_lyrics_result
    mock_rebuild_task.s.return_value = mock_rebuild_vectors_result

    monkeypatch.setattr(tasks, "resolve_track_id", mock_resolve_task)
    monkeypatch.setattr(tasks, "download_audio", mock_download_task)
    monkeypatch.setattr(tasks, "analyze_audio", mock_analyze_task)
    monkeypatch.setattr(tasks, "analyze_harmony", mock_harmony_task)
    monkeypatch.setattr(tasks, "upgrade_timings", mock_upgrade_task)
    monkeypatch.setattr(tasks, "sync_lyrics", mock_sync_task)
    monkeypatch.setattr(tasks, "rebuild_vectors", mock_rebuild_task)

    # Mock celery.chain and its apply_async method
    mock_chain_instance = MagicMock()
    mock_chain_apply_async = MagicMock()
    mock_chain_instance.apply_async = mock_chain_apply_async
    mock_chain = MagicMock(return_value=mock_chain_instance)
    monkeypatch.setattr(tasks, "chain", mock_chain)

    # include_timings=True exercises the full chain; the timing upgrade is
    # opt-in, so the default chain is asserted separately below.
    payload = {"artist": "A", "title": "B", "url": "http://youtube.com/vid1",
               "include_timings": True}
    tasks.postprocess_track.run(payload)

    # Assert that .s() was called on each task with the correct payload/context
    mock_resolve_task.s.assert_called_once_with(payload)
    mock_download_task.s.assert_called_once_with() # Download does not take previous result as arg
    mock_analyze_task.s.assert_called_once_with() # Analyze does not take previous result as arg
    mock_harmony_task.s.assert_called_once_with() # Harmony does not take previous result as arg
    mock_upgrade_task.s.assert_called_once_with() # Upgrade does not take previous result as arg
    mock_sync_task.s.assert_called_once_with() # Sync does not take previous result as arg
    mock_rebuild_task.s.assert_called_once_with() # Rebuild does not take previous result as arg

    # Assert that chain was called with the correct tasks (signatures)
    mock_chain.assert_called_once_with(
        mock_resolve_track_id_result, # The actual call is resolve_track_id.s(payload).set(immutable=True)
        mock_download_task.s.return_value,
        mock_analyze_task.s.return_value,
        mock_harmony_task.s.return_value,
        mock_upgrade_task.s.return_value,
        mock_sync_task.s.return_value,
        mock_rebuild_task.s.return_value
    )
    # Assert that apply_async was called on the chained object
    mock_chain_apply_async.assert_called_once_with()


def _chained_task_names(monkeypatch, payload):
    """Run the orchestrator and report which tasks it chained, in order."""
    chained = []

    class FakeChain:
        def __init__(self, *signatures):
            chained.extend(s.task for s in signatures)

        def apply_async(self, *a, **k):
            return MagicMock()

    monkeypatch.setattr(tasks, "chain", FakeChain)
    tasks.postprocess_track.run(payload)
    return [name.split(".")[-1] for name in chained]


def test_default_postprocess_leaves_out_the_timing_upgrade(monkeypatch):
    """upgrade_timings is rate-limited to 15/m; on by default it stalls the queue.

    It only runs when a caller explicitly asks (the TUI's "A" key), so the
    normal playback-driven chain must not contain it.
    """
    names = _chained_task_names(
        monkeypatch, {"artist": "A", "title": "B", "url": "http://youtube.com/v"})
    assert "upgrade_timings" not in names
    assert names == ["resolve_track_id", "download_audio", "analyze_audio",
                     "analyze_harmony", "sync_lyrics", "rebuild_vectors"]


def test_asking_for_timings_puts_the_upgrade_back_in_order(monkeypatch):
    names = _chained_task_names(
        monkeypatch, {"artist": "A", "title": "B", "url": "http://youtube.com/v",
                      "include_timings": True})
    assert names == ["resolve_track_id", "download_audio", "analyze_audio",
                     "analyze_harmony", "upgrade_timings", "sync_lyrics",
                     "rebuild_vectors"]


def test_enqueue_postprocess_uses_orchestrator(monkeypatch):
    with patch.object(tasks.postprocess_track, "delay", return_value=MagicMock(id="task-id-123")) as mock_delay:
        payload = {"artist": "Test", "title": "Song", "url": "http://example.com",
                   "include_timings": False, "full": False}
        monkeypatch.setenv("KARAOKE_ORCHESTRATOR", "celery") # Ensure celery path is taken
        task_id = q.publish_postprocess_task("Test", "Song", "http://example.com")
        mock_delay.assert_called_once_with(payload) # This asserts that the orchestrator task was called with .delay()
        assert task_id == "task-id-123"


def test_celery_emits_task_events_for_argo_events():
    assert celery_app.app.conf.worker_send_task_events is True
    assert celery_app.app.conf.task_send_sent_event is True
    assert celery_app.app.conf.task_track_started is True


def test_postprocess_context_does_not_mutate_input():
    import json
    input_dict = {
        "artist": "Artist",
        "title": "Song",
        "url": "https://example.com/audio.mp3",
        "audio_path": "/tmp/audio.mp3",
        "pending": ["analysis", "sync"],
    }
    ctx = PostprocessContext.from_dict(input_dict)
    # Ensure input_dict was not mutated
    assert isinstance(input_dict["audio_path"], str)
    assert input_dict["audio_path"] == "/tmp/audio.mp3"

    # Ensure ctx fields
    assert isinstance(ctx.audio_path, Path)

    # Ensure to_dict produces valid JSON
    serialized = ctx.to_dict()
    assert isinstance(serialized["audio_path"], str)
    json_bytes = json.dumps(serialized)
    assert "/tmp/audio.mp3" in json_bytes


# --- chords must never pull audio over the network ---------------------
def test_chords_alone_never_triggers_a_download(monkeypatch):
    """The library is 90% built from disk.

    Letting a missing chord analysis pull a download would mean re-fetching the
    whole library to analyse files already on the drive. Harmony rides along on
    audio that is already present; bulk chord work belongs to folder_scan.
    """
    called = []
    monkeypatch.setattr(postprocess_worker, "run_download_logic",
                        lambda *a, **k: called.append(1) or None)
    for pending in (["chords"], ["vectors"], ["chords", "vectors"]):
        context = PostprocessContext(artist="A", title="B",
                                     url="http://youtube.com/v", track_id=1,
                                     pending=list(pending))
        tasks.download_audio.run(context.to_dict())
    assert called == []


def test_audio_needed_for_analysis_still_downloads_and_harmony_rides_along(monkeypatch, tmp_path):
    """When something else already paid for the download, harmony is free."""
    monkeypatch.setattr(postprocess_worker, "run_download_logic",
                        lambda *a, **k: tmp_path / "got.mp3")
    context = PostprocessContext(artist="A", title="B",
                                 url="http://youtube.com/v", track_id=1,
                                 pending=["analysis", "chords"])
    result = PostprocessContext.from_dict(tasks.download_audio.run(context.to_dict()))
    assert result.audio_path == tmp_path / "got.mp3"
    assert "chords" in result.pending


def test_harmony_is_skipped_without_audio_already_present(monkeypatch):
    ran = []
    monkeypatch.setattr(postprocess_worker, "run_harmony_logic",
                        lambda *a, **k: ran.append(1) or True)
    monkeypatch.setattr(localcache, "connect", MagicMock())
    context = PostprocessContext(artist="A", title="B", url="u", track_id=1,
                                 audio_path=None, pending=["chords"])
    tasks.analyze_harmony.run(context.to_dict())
    assert ran == []


def test_full_run_may_fetch_audio_for_chords(monkeypatch, tmp_path):
    """Playlist imports are a bounded, explicitly chosen set.

    They opt into downloads so they come out fully processed; the library-wide
    default still refuses, which is what keeps a 13k-track chord backfill from
    re-downloading files already on disk.
    """
    monkeypatch.setattr(postprocess_worker, "run_download_logic",
                        lambda *a, **k: tmp_path / "got.mp3")
    context = PostprocessContext(artist="A", title="B",
                                 url="http://youtube.com/v", track_id=1,
                                 pending=["chords"], full=True)
    result = PostprocessContext.from_dict(tasks.download_audio.run(context.to_dict()))
    assert result.audio_path == tmp_path / "got.mp3"

    # ...and without the flag, the same track downloads nothing.
    called = []
    monkeypatch.setattr(postprocess_worker, "run_download_logic",
                        lambda *a, **k: called.append(1) or None)
    plain = PostprocessContext(artist="A", title="B",
                               url="http://youtube.com/v", track_id=1,
                               pending=["chords"], full=False)
    tasks.download_audio.run(plain.to_dict())
    assert called == []


def test_full_flag_survives_the_payload_round_trip(monkeypatch):
    """resolve_track_id builds the context, so the flag has to come off the payload."""
    monkeypatch.setattr(localcache, "find_track_id", MagicMock(return_value=5))
    monkeypatch.setattr(localcache, "find_track_by_url", MagicMock(return_value=None))
    monkeypatch.setattr(localcache, "extract_youtube_id", MagicMock(return_value="v"))
    monkeypatch.setattr(q, "needs_postprocessing", MagicMock(return_value=["chords"]))
    monkeypatch.setattr(localcache, "connect", MagicMock())

    for flag in (True, False):
        out = tasks.resolve_track_id.run(
            {"artist": "A", "title": "B", "url": "http://youtube.com/v", "full": flag})
        assert PostprocessContext.from_dict(out).full is flag
