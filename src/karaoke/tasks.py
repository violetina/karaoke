from __future__ import annotations

from typing import Any, cast
from celery import chain # For Pyright
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .celery_app import app
from .logger import log


@app.task(
    bind=True,
    name="karaoke.tasks.postprocess_track",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=2,
    # postprocess_track is the orchestrator; its return is for Flower status
    # and not used for chaining.
    ignore_result=True,
)
def postprocess_track(self, payload: dict[str, Any]) -> None:
    """Orchestrate one post-processing job from payload `{artist, title, url}`.

    Payload shape is the same as the legacy queue body: `{artist, title, url}`.
    The task is deliberately small: all DB idempotency, download decisions,
    analysis, lyric sync, timing upgrades and failure classification stay in
    `postprocess_worker.process_task()` for phase 1.
    """
    artist = (payload.get("artist") or "").strip()
    title = (payload.get("title") or "").strip()
    log.info("celery postprocess: received %s - %s", artist, title)

    # This is the orchestrator: it resolves the track, determines pending tasks,
    # then chains the sub-tasks in sequence. If a sub-task fails, Celery's
    # retry/backoff takes over.
    # This does not catch RuntimeErrors because it wants them to propagate so
    # Celery can retry the whole task chain. Only malformed payloads should be
    # ignored here.
    try:
        context = resolve_track_id.s(payload).set(immutable=True)
        tasks = [
            context,
            download_audio.s(),
            analyze_audio.s(),
            upgrade_timings.s(),
            sync_lyrics.s(),
            rebuild_vectors.s(),
        ]
        # The final task in the chain returns the status to the orchestrator,
        # but the orchestrator itself returns None (ignore_result=True).
        # Individual sub-tasks in the chain may return metadata to Flower if
        # they set ignore_result=False, but for phase 1 we keep them lean.
        chain(*tasks).apply_async()
    except Exception:
        log.exception("postprocess: orchestration failed for %s - %s", artist, title)


@dataclass
class PostprocessContext:
    """Context passed between chained Celery tasks."""
    artist: str
    title: str
    url: Optional[str]
    track_id: Optional[int] = None
    audio_path: Optional[Path] = None
    cookies_from_browser: Optional[str] = None
    pending: list[str] = field(default_factory=list)

    # Methods for easy serialization/deserialization to/from dict for Celery
    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["audio_path"] = str(self.audio_path) if self.audio_path else None
        # Convert Path objects to strings for serialization
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "PostprocessContext":
        # Convert string paths back to Path objects on deserialization
        d["audio_path"] = Path(d["audio_path"]) if d.get("audio_path") else None
        d["pending"] = d.get("pending", []) # Ensure pending is always a list
        return PostprocessContext(**d)


@app.task(
    bind=True,
    name="karaoke.tasks.resolve_track_id",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=1,
    ignore_result=False,
)
def resolve_track_id(self, payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve track_id and URL from payload; determine pending tasks."""
    import os # Redundant import for Pyright
    from . import localcache
    from .postprocess_queue import needs_postprocessing

    artist = (payload.get("artist") or "").strip()
    title = (payload.get("title") or "").strip()
    url = (payload.get("url") or "").strip()
    cookies = os.environ.get("KARAOKE_COOKIES_FROM_BROWSER")

    context = PostprocessContext(
        artist=artist, title=title, url=url, cookies_from_browser=cookies
    )

    with localcache.connect() as conn:
        track_id = localcache.find_track_id(artist, title, conn)
        if track_id is None and url:
            found = localcache.find_track_by_url(url, conn)
            if found:
                track_id = found[0]
        if track_id is None:
            log.warning("postprocess: track not found for %s - %s; skipping", artist, title)
            # This is a terminal failure, no need to retry or continue.
            raise ValueError(f"Track not found: {artist} - {title}")
        context.track_id = track_id

        # Prefer a real watch source from the DB over a non-watchable payload URL
        if not localcache.extract_youtube_id(url):
            url = ""
        if not url:
            row = conn.execute(
                """
                SELECT url FROM sources
                WHERE track_id = ? AND kind IN ('youtube', 'youtube_music')
                ORDER BY CASE WHEN kind = 'youtube_music' THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (track_id,),
            ).fetchone()
            if row:
                context.url = row[0]
        context.pending = needs_postprocessing(track_id, conn)

    return context.to_dict() # Return dict for serialization


@app.task(bind=True, name="karaoke.tasks.download_audio", autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=2)
def download_audio(self, context_dict: dict[str, Any]) -> dict[str, Any]:
    context = PostprocessContext.from_dict(context_dict)
    if context.track_id is None or context.url is None or ("analysis" not in context.pending and "sync" not in context.pending):
        return context.to_dict()
    log.info("celery download_audio: %s - %s", context.artist, context.title)
    from . import postprocess_worker
    downloaded_audio = postprocess_worker.run_download_logic(context.url, context.cookies_from_browser)
    if downloaded_audio:
        context.audio_path = downloaded_audio
    else:
        log.warning("postprocess: audio unavailable for track %s (%s)",
                    context.track_id, context.url)
        # If download fails, remove analysis/sync from pending tasks for this run
        if "analysis" in context.pending: context.pending.remove("analysis")
        if "sync" in context.pending: context.pending.remove("sync")
    return context.to_dict()


@app.task(bind=True, name="karaoke.tasks.analyze_audio", autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=2)
def analyze_audio(self, context_dict: dict[str, Any]) -> dict[str, Any]:
    context = PostprocessContext.from_dict(context_dict)
    if context.track_id is None or "analysis" not in context.pending or context.audio_path is None:
        return context.to_dict()
    log.info("celery analyze_audio: %s - %s", context.artist, context.title)
    from . import postprocess_worker, localcache # Import localcache for database connection
    with localcache.connect() as conn:
        if not postprocess_worker.run_analysis_logic(context.track_id, context.audio_path, conn):
            log.warning("postprocess: analysis failed for track %s (%s)",
                        context.track_id, context.url)
    return context.to_dict()


@app.task(bind=True, name="karaoke.tasks.upgrade_timings", autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=2)
def upgrade_timings(self, context_dict: dict[str, Any]) -> dict[str, Any]:
    context = PostprocessContext.from_dict(context_dict)
    if context.track_id is None or "timings" not in context.pending:
        return context.to_dict()
    log.info("celery upgrade_timings: %s - %s", context.artist, context.title)
    from . import postprocess_worker, localcache
    with localcache.connect() as conn:
        status = postprocess_worker.run_timings_logic(context.track_id, conn, context.cookies_from_browser)
        if status == "error":
            log.warning("postprocess: timings upgrade failed for track %s (%s)",
                        context.track_id, context.url)
            raise RuntimeError(f"Timings upgrade failed for track {context.track_id}") # Retryable error
    return context.to_dict()


@app.task(bind=True, name="karaoke.tasks.sync_lyrics", autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=2)
def sync_lyrics(self, context_dict: dict[str, Any]) -> dict[str, Any]:
    context = PostprocessContext.from_dict(context_dict)
    if context.track_id is None or "sync" not in context.pending or context.audio_path is None:
        return context.to_dict()
    log.info("celery sync_lyrics: %s - %s", context.artist, context.title)
    from . import postprocess_worker, localcache
    with localcache.connect() as conn:
        if not postprocess_worker.run_sync_logic(context.track_id, context.audio_path, conn):
            log.warning("postprocess: lyrics sync failed for track %s (%s)",
                        context.track_id, context.url)
            raise RuntimeError(f"Lyrics sync failed for track {context.track_id}") # Retryable error
    return context.to_dict()


@app.task(bind=True, name="karaoke.tasks.rebuild_vectors", autoretry_for=(Exception,), retry_backoff=True, retry_jitter=True, max_retries=2)
def rebuild_vectors(self, context_dict: dict[str, Any]) -> dict[str, Any]:
    context = PostprocessContext.from_dict(context_dict)
    if context.track_id is None or "vectors" not in context.pending:
        return context.to_dict()
    log.info("celery rebuild_vectors: %s - %s", context.artist, context.title)
    from . import postprocess_worker, localcache
    with localcache.connect() as conn:
        if not postprocess_worker.run_vectors_logic(context.track_id, conn):
            log.warning("postprocess: vectors rebuild failed for track %s (%s)",
                        context.track_id, context.url)
            raise RuntimeError(f"Vectors rebuild failed for track {context.track_id}") # Retryable error
    return context.to_dict()


def enqueue_postprocess(payload: dict[str, Any]) -> str:
    """Publish a Celery post-processing task and return its task id."""
    result = postprocess_track.delay(payload)
    return str(result.id)
