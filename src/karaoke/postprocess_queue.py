"""Asynchronous post-processing task publisher using RabbitMQ.

The post-processing pipeline fills in two derived assets for a track that the
foreground app doesn't compute inline:

- **Audio analysis** (musical key + tempo/BPM + energy/brightness), stored in
  ``track_analysis``. Requires the downloaded audio file in the YouTube cache.
- **Word-level timing** (Enhanced LRC), upgrading line-level synced lyrics using
  YouTube json3 captions.

``needs_postprocessing`` reports what is missing; ``enqueue_if_needed`` publishes
a task to RabbitMQ when anything is; the ``postprocess_worker`` consumes them.
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Optional

import pika

from .logger import log

QUEUE_NAME = "karaoke-postprocess"


def needs_postprocessing(track_id: int, conn: sqlite3.Connection) -> list[str]:
    """Return the list of pending post-processing tasks for a track.

    Possible values: ``"analysis"`` (no key/BPM row) and ``"timings"``
    (approved synced lyrics lack Enhanced LRC word tags). Empty list = nothing
    to do.
    """
    from . import track_analysis
    from .upgrade_timings import has_word_timings

    pending: list[str] = []
    cur = conn.cursor()

    # 1. Audio analysis (key/BPM). Missing if there's no track_analysis row.
    try:
        track_analysis.ensure_schema(conn)
        analysis = track_analysis.get_analysis(track_id, conn)
        if analysis is None or not analysis.bpm:
            pending.append("analysis")
    except Exception:
        pending.append("analysis")

    # 2. Word-level timing. Missing if approved synced lyrics carry no word tags.
    cur.execute(
        "SELECT synced_lyrics FROM lyrics WHERE track_id = ? AND kind = 'approved'",
        (track_id,),
    )
    row = cur.fetchone()
    if row and row[0] and not has_word_timings(row[0]):
        pending.append("timings")

    return pending


# What the worker can actually fetch audio (or captions) for. Both task kinds
# need it: analysis needs the file to examine, and the word-timing upgrade needs
# YouTube captions.
_DOWNLOADABLE_MARKERS = ("watch?v=", "youtu.be/", "youtube.com/embed/")


def is_downloadable(url: str) -> bool:
    """Whether the worker could obtain audio from this URL."""
    return any(marker in (url or "").lower() for marker in _DOWNLOADABLE_MARKERS)


def has_downloadable_source(track_id: int, conn: sqlite3.Connection) -> bool:
    """Whether any stored source for this track yields audio.

    A Spotify URL does not: there is no file behind it, which is the whole
    reason ``karaoke-sample`` and record mode exist.
    """
    try:
        rows = conn.execute(
            "SELECT url, kind FROM sources WHERE track_id = ?", (track_id,)
        ).fetchall()
    except sqlite3.Error:
        return False
    for row in rows:
        if row["kind"] == "local":
            return True
        if is_downloadable(row["url"] or ""):
            return True
    return False


def enqueue_if_needed(
    artist: str, title: str, url: str = "",
    conn: Optional[sqlite3.Connection] = None,
) -> bool:
    """Publish a post-processing task only if the track actually needs work.

    Looks up the track by artist/title, checks ``needs_postprocessing``, and
    enqueues when non-empty. Best-effort; never raises to the caller.
    """
    from . import localcache

    own = conn is None
    c = conn or localcache.connect()
    try:
        track_id = localcache.find_track_id(artist, title, c)
        if track_id is None:
            # Unknown track: still enqueue so the worker can resolve+download
            # it -- but only if there is something to download. A Spotify URL
            # would just fail on every retry.
            if url and not is_downloadable(url):
                log.debug("not enqueuing %s - %s: no downloadable source",
                          artist, title)
                return False
            return publish_postprocess_task(artist, title, url)
        pending = needs_postprocessing(track_id, c)
        if not pending:
            return False
        # The worker cannot analyse what it cannot fetch. Without this, every
        # Spotify-only track is enqueued, fails "no watchable URL", is retried
        # and dropped -- on every track change, forever. Those tracks need a
        # recording instead (karaoke-sample, or record mode).
        if not (is_downloadable(url) or has_downloadable_source(track_id, c)):
            log.info("skipping post-process for %s - %s: no downloadable audio"
                     " (sample it instead)", artist, title)
            return False
        return publish_postprocess_task(artist, title, url)
    except Exception as exc:
        log.debug("enqueue_if_needed skipped: %s", exc)
        return False
    finally:
        if own:
            c.close()


def publish_postprocess_task(artist: str, title: str, url: str = "") -> bool:
    """Publish a track post-processing task to the RabbitMQ queue.

    Returns True if successfully published, False otherwise.
    Safe: catches pika exceptions so the TUI/app never crashes.
    """
    if not (artist or title):
        return False

    host = os.environ.get("RABBITMQ_HOST", "localhost")
    user = os.environ.get("RABBITMQ_USER", "guest")
    password = os.environ.get("RABBITMQ_PASS", "guest")

    try:
        credentials = pika.PlainCredentials(user, password)
        # Bounded 3s connection timeout so we don't hang the TUI thread if unreachable
        parameters = pika.ConnectionParameters(
            host=host,
            credentials=credentials,
            connection_attempts=1,
            retry_delay=1,
            socket_timeout=3,
        )
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()
        channel.queue_declare(queue=QUEUE_NAME, durable=True)

        payload = {
            "artist": artist.strip(),
            "title": title.strip(),
            "url": url.strip() if url else ""
        }
        
        channel.basic_publish(
            exchange="",
            routing_key=QUEUE_NAME,
            body=json.dumps(payload),
            properties=pika.BasicProperties(
                delivery_mode=2
            )
        )
        connection.close()
        log.info("Published post-processing task to RabbitMQ: %s - %s", artist, title)
        return True
    except Exception as exc:
        log.debug("RabbitMQ publish skipped (unreachable/failed): %s", exc)
        return False
