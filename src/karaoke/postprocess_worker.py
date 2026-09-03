"""RabbitMQ consumer that runs deferred track post-processing.

Consumes tasks published by :mod:`karaoke.postprocess_queue` and, per track:

1. Ensures the audio is downloaded to the YouTube cache.
2. Runs key/BPM/energy analysis and stores it in ``track_analysis`` (if missing).
3. Upgrades line-level synced lyrics to Enhanced LRC word timing via YouTube
   json3 captions (if missing).

The queue is intentionally NON-persistent conceptually: if the broker is reset
we can simply re-enqueue from SQLite. Tasks are ACKed only after processing so a
crash re-delivers them.

Run:  ``karaoke-postprocess-worker``  (or ``python -m karaoke.postprocess_worker``)
Env:  RABBITMQ_HOST (default localhost), RABBITMQ_USER/PASS (default guest).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import pika

from . import localcache, track_analysis
from .config import settings
from .logger import log
from .postprocess_queue import QUEUE_NAME, needs_postprocessing


def _cache_path_for_url(url: str) -> Optional[Path]:
    """Return the local cache path for a YouTube URL's video, if present."""
    vid = localcache.extract_youtube_id(url)
    if not vid:
        return None
    for ext in (".webm", ".m4a", ".mp3", ".opus", ".ogg"):
        p = Path(settings.youtube_dir) / f"{vid}{ext}"
        if p.is_file():
            return p
    return None


def _ensure_download(url: str, cookies_from_browser: Optional[str]) -> Optional[Path]:
    """Ensure the audio for a YouTube URL is in the cache; download if needed."""
    existing = _cache_path_for_url(url)
    if existing:
        return existing
    try:
        from .youtube import fetch_metadata
        meta = fetch_metadata(url, download=True, cookies_from_browser=cookies_from_browser)
        path = meta.get("path")
        if path and Path(path).is_file():
            return Path(path)
    except Exception:
        log.exception("postprocess: download failed for %s", url)
    return _cache_path_for_url(url)


def _run_analysis(track_id: int, audio_path: Path, conn) -> bool:
    """Run key/BPM/energy analysis on a local file and persist it."""
    try:
        from .analyze import analyze_audio
        result = analyze_audio(str(audio_path))
        track_analysis.save_detected(
            track_id,
            detected_key=result.key,
            key_confidence=result.key_confidence,
            key_agreement=result.key_agreement,
            bpm=result.bpm,
            method=result.method,
            energy=result.energy,
            brightness=result.brightness,
            analyzer_version=result.version,
            conn=conn,
        )
        log.info("postprocess: analyzed track %s (key=%s bpm=%s)",
                 track_id, result.key.name if result.key else "?", result.bpm)
        return True
    except Exception:
        log.exception("postprocess: analysis failed for track %s", track_id)
        return False


def _run_timings(track_id: int, conn, cookies_from_browser: Optional[str]) -> bool:
    """Upgrade a track's synced lyrics to Enhanced LRC word timing."""
    try:
        from .upgrade_timings import upgrade_track
        row = conn.execute(
            """
            SELECT t.track_id, t.artist, t.title, t.album, t.duration,
                   s.url, l.synced_lyrics, l.plain_lyrics
            FROM tracks t
            JOIN sources s ON s.track_id = t.track_id AND s.kind IN ('youtube', 'youtube_music')
            JOIN lyrics l  ON l.track_id = t.track_id AND l.kind = 'approved'
            WHERE t.track_id = ?
            LIMIT 1
            """,
            (track_id,),
        ).fetchone()
        if row is None:
            return False
        res = upgrade_track(row, conn, cookies_from_browser=cookies_from_browser)
        log.info("postprocess: timings upgrade for track %s -> %s", track_id, res.status)
        return res.status == "upgraded"
    except Exception:
        log.exception("postprocess: timing upgrade failed for track %s", track_id)
        return False


def process_task(payload: dict) -> None:
    """Process one post-processing task payload {artist, title, url}."""
    artist = (payload.get("artist") or "").strip()
    title = (payload.get("title") or "").strip()
    url = (payload.get("url") or "").strip()
    cookies = os.environ.get("KARAOKE_COOKIES_FROM_BROWSER")

    log.info("postprocess: received task for %s - %s", artist, title)
    with localcache.connect() as conn:
        track_id = localcache.find_track_id(artist, title, conn)
        if track_id is None and url:
            found = localcache.find_track_by_url(url, conn)
            if found:
                track_id = found[0]
        if track_id is None:
            log.warning("postprocess: track not found for %s - %s; skipping", artist, title)
            return

        pending = needs_postprocessing(track_id, conn)
        if not pending:
            log.info("postprocess: nothing pending for track %s", track_id)
            return

        # Prefer a real watch source from the DB over a non-watchable payload URL
        # (e.g. a youtube search results page carries no extractable video id).
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
                url = row[0]

        if "analysis" in pending and url:
            audio = _ensure_download(url, cookies)
            if audio:
                _run_analysis(track_id, audio, conn)

        if "timings" in pending:
            _run_timings(track_id, conn, cookies)


def main() -> int:
    """Run the blocking RabbitMQ consumer loop."""
    host = os.environ.get("RABBITMQ_HOST", "localhost")
    user = os.environ.get("RABBITMQ_USER", "guest")
    password = os.environ.get("RABBITMQ_PASS", "guest")

    credentials = pika.PlainCredentials(user, password)
    parameters = pika.ConnectionParameters(
        host=host, credentials=credentials,
        heartbeat=600, blocked_connection_timeout=300,
    )
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    channel.queue_declare(queue=QUEUE_NAME, durable=True)
    channel.basic_qos(prefetch_count=1)

    def _callback(ch, method, properties, body):
        try:
            payload = json.loads(body)
            process_task(payload)
        except Exception:
            log.exception("postprocess: task failed")
        finally:
            ch.basic_ack(delivery_tag=method.delivery_tag)

    channel.basic_consume(queue=QUEUE_NAME, on_message_callback=_callback)
    log.info("postprocess worker listening on %s@%s queue=%s", user, host, QUEUE_NAME)
    print(f"Post-processing worker listening on queue '{QUEUE_NAME}' (host={host}). Ctrl-C to stop.")
    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        channel.stop_consuming()
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
