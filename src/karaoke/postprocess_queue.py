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
import psycopg
from psycopg import Connection, Cursor
from typing import Optional

import pika

from .logger import log

QUEUE_NAME = "karaoke-postprocess"
ORCHESTRATOR_ENV = "KARAOKE_ORCHESTRATOR"


def orchestrator() -> str:
    """Selected background orchestrator (`celery` by default, `legacy` fallback)."""
    return os.environ.get(ORCHESTRATOR_ENV, "celery").strip().lower() or "celery"


def needs_postprocessing(track_id: int, conn: Connection, *,
                         include_search_artefacts: bool = True) -> list[str]:
    """Return the list of pending post-processing tasks for a track.

    Possible values: ``"analysis"`` (no key/BPM row), ``"timings"`` (approved
    synced lyrics lack Enhanced LRC word tags), ``"sync"`` (approved lyrics are
    plain text with no timings at all), ``"vectors"`` (no OpenSearch document)
    and ``"chords"`` (no harmonic progression with chord motion). Empty list =
    nothing to do.

    The last two are answered by OpenSearch rather than the database. Pass
    ``include_search_artefacts=False`` to skip those two round-trips when the
    caller only cares about the database-derived steps.
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

    # 2. Word-level timing. Missing if approved synced lyrics carry no word tags,
    # unless we already verified that YouTube has no word-level captions for it.
    cur.execute(
        "SELECT synced_lyrics, plain_lyrics, source FROM lyrics"
        " WHERE track_id = %s AND kind = 'approved'",
        (track_id,),
    )
    row = cur.fetchone()
    if (
        row
        and row["synced_lyrics"]
        and not has_word_timings(row["synced_lyrics"])
        and row.get("source") != "no_youtube_captions"
    ):
        pending.append("timings")

    # 3. Any timing at all. Words with no timestamps cannot drive a karaoke
    # session -- and they are now arriving routinely from the player's own
    # lyrics panel, which supplies text and nothing else. Whisper can give
    # them a rhythm without being trusted for the words themselves.
    if row and not row["synced_lyrics"] and row["plain_lyrics"]:
        pending.append("sync")

    # 4. Search artefacts in OpenSearch: the track's own vector document, and
    # the harmonic progression/chord document. Both are best-effort -- if the
    # cluster is unreachable we report nothing rather than claim every track in
    # the library needs re-indexing, which would flood the queue.
    if include_search_artefacts:
        pending.extend(_missing_search_artefacts(track_id))

    return pending


def _missing_search_artefacts(track_id: int) -> list[str]:
    """Return which of ``vectors``/``chords`` are absent for this track.

    Never raises and never guesses: an unreachable cluster yields an empty list,
    so a broker/OpenSearch outage cannot enqueue the whole library.
    """
    from .config import settings
    from .key_progression import PROGRESSION_INDEX, doc_id as progression_doc_id
    from .vector_index import track_doc_id

    try:
        from .osclient import client as get_os_client
        os_client = get_os_client()
    except Exception:
        return []
    if os_client is None:
        return []

    missing: list[str] = []
    try:
        if not os_client.exists(index=settings.index_name,
                                id=track_doc_id(track_id)):
            missing.append("vectors")
    except Exception:
        log.debug("could not check vector doc for track %s", track_id)
    try:
        # A progression document can predate chord detection, so the chord
        # field has to be checked rather than the document's existence.
        res = os_client.search(index=PROGRESSION_INDEX, body={
            "size": 1, "_source": False,
            "query": {"bool": {"filter": [
                {"ids": {"values": [progression_doc_id(track_id)]}},
                {"exists": {"field": "chord_motion"}},
            ]}},
        })
        if not res["hits"]["hits"]:
            missing.append("chords")
    except Exception:
        log.debug("could not check progression doc for track %s", track_id)
    return missing


# What the worker can actually fetch audio (or captions) for. Both task kinds
# need it: analysis needs the file to examine, and the word-timing upgrade needs
# YouTube captions.
_DOWNLOADABLE_MARKERS = ("watch?v=", "youtu.be/", "youtube.com/embed/")


def is_downloadable(url: str) -> bool:
    """Whether the worker could obtain audio from this URL."""
    return any(marker in (url or "").lower() for marker in _DOWNLOADABLE_MARKERS)


def has_downloadable_source(track_id: int, conn: Connection) -> bool:
    """Whether any stored source for this track yields audio.

    A Spotify URL does not: there is no file behind it, which is the whole
    reason ``karaoke-sample`` and record mode exist.
    """
    try:
        rows = conn.execute(
            "SELECT url, kind FROM sources WHERE track_id = %s", (track_id,)
        ).fetchall()
    except psycopg.Error:
        return False
    for row in rows:
        if row["kind"] == "local":
            return True
        if is_downloadable(row["url"] or ""):
            return True
    return False


def enqueue_if_needed(
    artist: str, title: str, url: str = "",
    conn: Optional[Connection] = None,
    include_timings: bool = False,
) -> bool:
    """Publish a post-processing task only if the track actually needs work.

    Looks up the track by artist/title, checks ``needs_postprocessing``, and
    enqueues when non-empty. Best-effort; never raises to the caller.

    ``include_timings`` is off by default, so the automatic playback-driven
    path never queues the rate-limited word-timing upgrade.
    """
    from . import localcache

    own = conn is None
    c = conn or localcache.connect()
    try:
        track_id = localcache.find_track_id(artist, title, c)
        if track_id is None:
            # Unknown track: still enqueue so the worker can resolve+download
            # it -- but only if there is something to download.
            if url and not is_downloadable(url):
                log.debug("not enqueuing %s - %s: no downloadable source",
                          artist, title)
                return False
            return publish_postprocess_task(artist, title, url, include_timings)
        pending = needs_postprocessing(track_id, c)
        if not pending:
            return False
        # Timings are opt-in, so a track that needs nothing else would run an
        # entire chain of no-ops. Skip it rather than queue busywork.
        if not include_timings and pending == ["timings"]:
            log.debug("not enqueuing %s - %s: only timings pending (opt-in)",
                      artist, title)
            return False
        # The worker cannot analyse what it cannot fetch. However, if the track
        # has plain lyrics needing sync or timing upgrades, we allow enqueuing
        # because Celery will automatically search YouTube to find a source!
        if not (is_downloadable(url) or has_downloadable_source(track_id, c)):
            if "sync" not in pending and "timings" not in pending:
                log.info("skipping post-process for %s - %s: no downloadable audio"
                         " (sample it instead)", artist, title)
                return False
        return publish_postprocess_task(artist, title, url, include_timings)
    except Exception as exc:
        log.debug("enqueue_if_needed skipped: %s", exc)
        return False
    finally:
        if own:
            c.close()


def publish_postprocess_task(artist: str, title: str, url: str = "",
                             include_timings: bool = False,
                             full: bool = False) -> bool:
    """Publish a track post-processing task to RabbitMQ.

    Returns True if successfully published, False otherwise.
    Safe: catches pika exceptions so the TUI/app never crashes.

    ``include_timings`` adds the word-timing upgrade to the chain. It is off by
    default: the upgrade is rate-limited to 15/m, so running it on every track
    stalls the whole post-processing pipeline. Pass True only when a caller
    explicitly asks for timings.

    ``full`` lets the chain download audio that only harmony needs. Off by
    default, because the library is 90% built from disk and a library-wide
    chord backfill would re-fetch files already on the drive. Pass True for a
    bounded, explicitly chosen set -- a playlist import.
    """
    if not (artist or title):
        return False

    payload = {
        "artist": artist.strip(),
        "title": title.strip(),
        "url": url.strip() if url else "",
        "include_timings": bool(include_timings),
        "full": bool(full),
    }

    if orchestrator() != "legacy":
        try:
            from .tasks import enqueue_postprocess
            task_id = enqueue_postprocess(payload)
            log.info("Published post-processing Celery task %s: %s - %s",
                     task_id, artist, title)
            return task_id
        except Exception as exc:
            log.debug("Celery publish skipped (unreachable/failed): %s", exc)
            return None

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
