"""RabbitMQ consumer that runs deferred track post-processing.

Consumes tasks published by :mod:`karaoke.postprocess_queue` and, per track:

1. Ensures the audio is downloaded to the YouTube cache.
2. Runs key/BPM/energy analysis and stores it in ``track_analysis`` (if missing).
3. Upgrades line-level synced lyrics to Enhanced LRC word timing via YouTube
   json3 captions (if missing).

The queue is intentionally NON-persistent conceptually: if the broker is reset
we can simply re-enqueue from SQLite. A task is ACKed only once it completes;
a failure is requeued once and then dropped, so a crash re-delivers work while a
permanently-failing track cannot spin the worker in a redelivery loop.

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


def _run_timings(track_id: int, conn, cookies_from_browser: Optional[str]) -> str:
    """Upgrade a track's synced lyrics to Enhanced LRC word timing.

    Returns the upgrade status: ``"upgraded"``, ``"no-captions"`` (terminal —
    the video simply has none), ``"no-source"`` (nothing joinable in the DB) or
    ``"error"`` (retryable).
    """
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
            log.info("postprocess: no youtube source + approved lyrics for track %s",
                     track_id)
            return "no-source"
        res = upgrade_track(row, conn, cookies_from_browser=cookies_from_browser)
        log.info("postprocess: timings upgrade for track %s -> %s", track_id, res.status)
        return res.status
    except Exception:
        log.exception("postprocess: timing upgrade failed for track %s", track_id)
        return "error"


def _run_sync(track_id: int, audio_path: Path, conn) -> bool:
    """Give plain lyrics a rhythm, by aligning them to a transcription.

    Whisper is here for **timing only**. Its words on sung audio are
    unreliable -- "up to do" becomes "up to doom", and it emits "\u266a"
    artifacts -- so where a real source supplied the text, those words are kept
    and only the timestamps are taken. This is the same rule
    upgrade_timings.upgrade_track follows for captions, and what makes the
    player's own lyrics panel useful: it has the words and no timings, and
    Whisper has the reverse.
    """
    from .lyric_align import align_lines
    from .lyrics import parse_lrc
    from .whisper_sync import lines_to_lrc, transcribe_to_words

    row = conn.execute(
        "SELECT plain_lyrics, source FROM lyrics"
        " WHERE track_id = ? AND kind = 'approved'", (track_id,)).fetchone()
    plain = (row["plain_lyrics"] or "").strip() if row else ""
    if not plain:
        return False

    # Where the *words* came from decides what this becomes. `whisper_aligned`
    # means real words with Whisper timings and is deliberately not demoted in
    # search; stamping it onto a track whose words are themselves a Whisper
    # guess would launder the guess into evidence and lose the "(guessed)"
    # label. Timing a transcription makes it singable, not corroborated.
    from .librarysearch import is_transcribed

    synced_source = ("whisper_synced" if is_transcribed(row["source"] or "")
                     else "whisper_aligned")

    try:
        # Tell Whisper the language rather than letting it detect one from the
        # opening of the audio, which on music is regularly an instrumental
        # intro. The lyrics are already in hand, so the answer is knowable --
        # and a wrong language is worse than none, since it produces fluent
        # words in the wrong vocabulary that then anchor nothing at all.
        # lyric_language.detect returns None when unsure, which restores the
        # previous behaviour exactly.
        from .lyric_language import detect as detect_language

        language = detect_language(plain)
        if language:
            log.info("postprocess: transcribing track %s as %s", track_id, language)
        words = transcribe_to_words(str(audio_path), text=plain, language=language)
        # track_analysis is created on demand, so the join below raises rather
        # than simply finding no tempo on a database that has never stored one.
        track_analysis.ensure_schema(conn)
        meta = conn.execute(
            "SELECT t.duration, a.bpm FROM tracks t"
            " LEFT JOIN track_analysis a ON a.track_id = t.track_id"
            " WHERE t.track_id = ?", (track_id,)).fetchone()
        # The tempo is what turns a plausible-looking timestamp into a
        # musically placed one: it sets the beat grid the lines snap to and the
        # bar window that identifies an instrumental break.
        # align_lines rather than align_lyrics_to_lrc so the support report
        # comes back with the timings: how many lines were anchored on a heard
        # word and how much of the track had no anchor at all. Once written to
        # an LRC the two are indistinguishable, and reconstructing them later
        # would mean transcribing again -- which Whisper does not do
        # reproducibly, so it would not describe the row that was stored.
        report: dict = {}
        lyric_lines = [ln.strip() for ln in plain.splitlines() if ln.strip()]
        lrc = lines_to_lrc(align_lines(
            lyric_lines, words,
            total_duration=(meta["duration"] if meta else None),
            bpm=(meta["bpm"] if meta else None),
            report=report))
    except Exception:
        log.exception("postprocess: sync failed for track %s", track_id)
        return False

    if not lrc.strip() or not parse_lrc(lrc):
        log.warning("postprocess: alignment produced no timings for track %s",
                    track_id)
        return False

    # Zero anchors is refused, and this is not the coverage gate the
    # measurement argued against. That argument was about *sparse* anchoring:
    # 45% anchored scored 0.77s while 54% scored 2.63s, so a low count predicts
    # uncertainty rather than error, and the timings are worth keeping and
    # flagging. With no anchors at all there is nothing to be uncertain about --
    # the lines are spaced evenly across the duration because nothing was heard,
    # and storing that marks the track synced and stops anything trying again.
    if report.get("lines") and not report.get("anchored"):
        log.warning("postprocess: no anchored line for track %s; "
                    "timings would be evenly spaced guesses, not stored",
                    track_id)
        return False

    conn.execute(
        "UPDATE lyrics SET synced_lyrics = ?, source = ?"
        " WHERE track_id = ? AND kind = 'approved'",
        (lrc, synced_source, track_id))
    conn.commit()

    # Stored whether the alignment looks well supported or not. This is a flag,
    # never a gate: the timings are kept either way, because the measurement
    # only justifies certifying a well-anchored alignment, not rejecting a
    # sparse one -- a 45%-anchored track scored 0.77s while a 54%-anchored one
    # scored 2.63s, so poor coverage predicts uncertainty, not error.
    localcache.ensure_alignment_support_table(conn)
    localcache.record_alignment_support(track_id, report, conn,
                                        source=synced_source)
    trustworthy = localcache.alignment_is_trustworthy(
        localcache.alignment_support(track_id, conn))
    log.info("postprocess: synced %d line(s) for track %s "
             "(%s/%s lines anchored%s)",
             len(parse_lrc(lrc)), track_id,
             report.get("anchored", "?"), report.get("lines", "?"),
             "" if trustworthy else " — worth a listen")
    return True


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

        failed: list[str] = []

        if "analysis" in pending:
            if not url:
                log.warning("postprocess: no watchable URL for track %s; "
                            "cannot run analysis", track_id)
                failed.append("analysis")
            else:
                audio = _ensure_download(url, cookies)
                if not audio:
                    log.warning("postprocess: audio unavailable for track %s (%s)",
                                track_id, url)
                    failed.append("analysis")
                elif not _run_analysis(track_id, audio, conn):
                    failed.append("analysis")

        if "sync" in pending:
            # Needs the whole track, not an excerpt: alignment spreads every
            # line across the full duration, so a sample would compress them.
            if not url:
                log.warning("postprocess: no watchable URL for track %s; "
                            "cannot sync lyrics", track_id)
                failed.append("sync")
            else:
                audio = _ensure_download(url, cookies)
                if not audio:
                    failed.append("sync")
                elif not _run_sync(track_id, audio, conn):
                    failed.append("sync")

        if "timings" in pending:
            # "no-captions"/"no-source" are terminal: retrying cannot help.
            if _run_timings(track_id, conn, cookies) == "error":
                failed.append("timings")

        if failed:
            # Signal the consumer so the task is redelivered rather than dropped.
            raise RuntimeError(
                f"post-processing incomplete for track {track_id}: {', '.join(failed)}"
            )


def handle_message(ch, method, body) -> None:
    """ACK/NACK one delivery according to whether its task completed.

    Success ACKs. A failure is requeued once; a delivery that already came back
    (``method.redelivered``) is treated as a poison task and dropped, so a
    permanently-failing track cannot spin the worker in a hot redelivery loop.
    A malformed body is dropped outright — retrying cannot make it parse.
    """
    try:
        payload = json.loads(body)
    except Exception:
        log.exception("postprocess: dropping malformed task body")
        ch.basic_ack(delivery_tag=method.delivery_tag)
        return

    try:
        process_task(payload)
    except Exception:
        log.exception("postprocess: task failed")
        if method.redelivered:
            log.error("postprocess: dropping task after retry: %s", payload)
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        else:
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
    else:
        ch.basic_ack(delivery_tag=method.delivery_tag)


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
        handle_message(ch, method, body)

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
