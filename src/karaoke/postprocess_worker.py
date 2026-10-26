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
import time
from pathlib import Path
from typing import Optional

import pika
from pika.exceptions import AMQPError

from . import localcache, track_analysis
from .config import settings
from .logger import log
from .postprocess_queue import QUEUE_NAME, needs_postprocessing


def _get_cached_audio_path(url: str) -> Optional[Path]:
    """Return the local cache path for a YouTube URL's video, if present."""
    vid = localcache.extract_youtube_id(url)
    if not vid:
        return None
    for ext in (".webm", ".m4a", ".mp3", ".opus", ".ogg"):
        p = Path(settings.youtube_dir) / f"{vid}{ext}"
        if p.is_file():
            return p
    return None


#: Shorter than this, a "song" file is a fragment, not the audio.
MIN_AUDIO_SECONDS = 5.0


def audio_file_ok(path: Path) -> bool:
    """Whether a cached file is readable audio of a plausible length.

    A download can leave a broken file that looks like any other: Frugit's
    "You Make Me Wait" was 312 KB of WebM with no valid header, and every step
    that read it failed while the cache kept handing it out. ffprobe reads the
    header only, so this costs tens of milliseconds, not a decode. Without
    ffprobe the file is trusted, as before.
    """
    import shutil
    import subprocess

    if not shutil.which("ffprobe"):
        return True
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", str(path)],
            capture_output=True, text=True, timeout=20)
        seconds = float(res.stdout.strip()) if res.returncode == 0 else 0.0
    except (subprocess.SubprocessError, ValueError, OSError):
        return False
    return seconds >= MIN_AUDIO_SECONDS


def _discard(path: Path, why: str) -> None:
    log.warning("postprocess: discarding %s from the cache: %s", path, why)
    try:
        path.unlink()
    except OSError:
        pass


def run_download_logic(url: str, cookies_from_browser: Optional[str]) -> Optional[Path]:
    """Ensure audio for a YouTube or local URL is in the cache; download if needed.
    This function can be called by Celery tasks directly.

    A cached file is only reused, and a download only returned, once it
    reads as audio (audio_file_ok); a broken one is deleted and fetched
    again rather than handed to every later step.
    """
    if url and Path(url).is_file():
        return Path(url)
    existing = _get_cached_audio_path(url)
    if existing:
        if audio_file_ok(existing):
            return existing
        _discard(existing, "cached file is not readable audio")
    try:
        from .youtube import fetch_metadata
        meta = fetch_metadata(url, download=True, cookies_from_browser=cookies_from_browser)
        downloaded_path_str = meta.get("path")
        if downloaded_path_str and Path(downloaded_path_str).is_file():
            downloaded = Path(downloaded_path_str)
            if audio_file_ok(downloaded):
                return downloaded
            _discard(downloaded, "download is not readable audio")
            return None
    except Exception:
        log.exception("postprocess: download failed for %s", url)

    fallback = _get_cached_audio_path(url)
    if fallback and audio_file_ok(fallback):
        return fallback
    if fallback:
        _discard(fallback, "file left by a failed download is not readable audio")
    return None


def run_analysis_logic(track_id: int, audio_path: Path, conn) -> bool:
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
            source_kind="postprocess",
            conn=conn,
        )
        log.info("postprocess: analyzed track %s (key=%s bpm=%s)",
                 track_id, result.key.name if result.key else "?", result.bpm)
        return True
    except Exception:
        log.exception("postprocess: analysis failed for track %s", track_id)
        return False


def run_timings_logic(track_id: int, conn, cookies_from_browser: Optional[str]) -> str:
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
            WHERE t.track_id = %s
            LIMIT 1
            """,
            (track_id,),
        ).fetchone()
        if row is None:
            log.info("postprocess: no youtube source + approved lyrics for track %s",
                     track_id)
            return "no-source"
        res = upgrade_track(row, conn, delay=3.0, cookies_from_browser=cookies_from_browser)
        log.info("postprocess: timings upgrade for track %s -> %s", track_id, res.status)
        if res.status == "upgraded":
            try:
                from .caption_sync import sync_audio_video_counterparts
                sync_audio_video_counterparts(track_id, conn, cookies_from_browser=cookies_from_browser)
            except Exception as exc:
                log.debug("postprocess: av sync failed for track %s: %s", track_id, exc)
        if res.status == "no-captions":
            try:
                conn.execute(
                    "UPDATE lyrics SET source = 'no_youtube_captions' WHERE track_id = %s AND kind = 'approved'",
                    (track_id,),
                )
                conn.commit()
            except Exception:
                pass
        return res.status
    except Exception as exc:
        if "rate limited" in str(exc).lower():
            log.warning("postprocess: rate-limited by YouTube for track %s: %s", track_id, exc)
            return "rate-limited"
        log.exception("postprocess: timing upgrade failed for track %s", track_id)
        return "error"


def run_sync_logic(track_id: int, audio_path: Path, conn) -> bool:
    """Give plain lyrics a rhythm, by aligning them to a transcription.

    Whisper is here for **timing only**. Its words on sung audio are
    unreliable -- "up to do" becomes "up to doom", and it emits "♪"
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
        "SELECT t.artist, t.title, l.plain_lyrics, l.source FROM tracks t"
        " JOIN lyrics l ON l.track_id = t.track_id"
        " WHERE t.track_id = %s AND l.kind = 'approved'", (track_id,)).fetchone()
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

    dur = None
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(str(audio_path))
        if mf and mf.info and hasattr(mf.info, "length") and mf.info.length:
            dur = float(mf.info.length)
    except Exception:
        pass
    if dur is None:
        try:
            import subprocess
            res = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
                capture_output=True, text=True, timeout=5, check=True,
            )
            dur = float(res.stdout.strip())
        except Exception:
            pass
    from .postprocess_queue import record_sync_failure

    if dur and dur > 900:  # 15 minutes cap for Whisper transcription
        log.warning("postprocess: skipping sync for track %s, duration %0.1fs exceeds 15min cap",
                    track_id, dur)
        record_sync_failure(track_id, plain, "too long for Whisper", conn)
        return False

    try:
        from .lyrics import fetch_lrclib
        ly = fetch_lrclib(row["artist"], row["title"], duration=dur)
        if ly.has_synced:
            log.info("postprocess: upgraded track %s to synced via online lyrics", track_id)
            conn.execute(
                "UPDATE lyrics SET synced_lyrics = %s, source = %s"
                " WHERE track_id = %s AND kind = 'approved'",
                (ly.synced_raw, ly.source, track_id))
            conn.commit()
            return True
    except Exception:
        log.warning("postprocess: fetch_lrclib failed for track %s", track_id, exc_info=True)

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
            " WHERE t.track_id = %s", (track_id,)).fetchone()
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
        
        # In case the plain lyrics were already doubled in the DB (or pasted as sync+plain)
        from .ytmusic_lyrics import _undouble
        lyric_lines = _undouble(lyric_lines)
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
        record_sync_failure(track_id, plain, "alignment produced no timings", conn)
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
        record_sync_failure(track_id, plain, "no line anchored on a heard word", conn)
        return False

    conn.execute(
        "UPDATE lyrics SET synced_lyrics = %s, source = %s"
        " WHERE track_id = %s AND kind = 'approved'",
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
    try:
        from .caption_sync import sync_audio_video_counterparts
        sync_audio_video_counterparts(track_id, conn)
    except Exception as exc:
        log.debug("postprocess: av sync failed for track %s: %s", track_id, exc)
    return True


def run_vectors_logic(track_id: int, conn) -> bool:
    """Rebuild audio/lyrics vectors for a track."""
    try:
        from .vector_index import index_track
        # One document, not a library rebuild: this runs per track at the end of
        # a post-processing chain. The previous call passed track_id to
        # rebuild_from_sqlite, which takes no such argument -- it raised
        # TypeError on every invocation.
        if not index_track(track_id, conn=conn):
            log.warning("postprocess: no vector doc written for track %s", track_id)
            return False
        log.info("postprocess: rebuilt vectors for track %s", track_id)
        return True
    except Exception:
        log.exception("postprocess: failed to rebuild vectors for track %s", track_id)
        return False


def run_harmony_logic(track_id: int, audio_path: Path, conn,
                      artist: str = "", title: str = "") -> bool:
    """Derive key movement and chord motion for a track and store them.

    The same pair folder_scan produces: a Progression describes how the key
    moves across the track, chord detection describes the harmony inside those
    regions. They answer different questions, so a failure to detect chords
    still stores the progression.
    """
    try:
        from . import chords, key_progression

        prog = key_progression.analyse(str(audio_path))
        if not prog:
            log.warning("postprocess: no progression for track %s (%s)",
                        track_id, audio_path)
            return False
        chord_analysis = None
        try:
            chord_analysis = chords.detect(str(audio_path))
        except Exception:
            log.debug("postprocess: chord detection failed for track %s",
                      track_id, exc_info=True)
        if not key_progression.store(track_id, prog, artist=artist, title=title,
                                     chords=chord_analysis):
            log.warning("postprocess: could not store progression for track %s", track_id)
            return False
        log.info("postprocess: analysed harmony for track %s (chords=%s)",
                 track_id, chord_analysis is not None)
        return True
    except Exception:
        log.exception("postprocess: harmony analysis failed for track %s", track_id)
        return False




def run_clap_logic(track_id: int, audio_path: Path, conn,
                   artist: str = "", title: str = "") -> bool:
    """Embed a track with CLAP and index the vector.

    Runs on its own single-process worker (``karaoke-clap`` queue): the model
    is about a gigabyte, so loading it in each of the six analysis workers
    would cost six.
    """
    try:
        from . import clap_vector, track_analysis

        if not clap_vector.available():
            log.warning("postprocess: CLAP unavailable (torch/transformers); "
                        "track %s not embedded", track_id)
            return False
        vector = clap_vector.embed_audio(str(audio_path))
        if vector is None:
            log.warning("postprocess: CLAP could not embed track %s (%s)",
                        track_id, audio_path)
            return False
        analysis = track_analysis.get_analysis(track_id, conn)
        key = getattr(analysis, "detected_key", None) if analysis else None
        row = conn.execute("SELECT album FROM tracks WHERE track_id = %s",
                           (track_id,)).fetchone()
        if not clap_vector.store(track_id, vector, artist=artist, title=title,
                                 album=(row["album"] if row else "") or "",
                                 detected_key=getattr(key, "name", "") or "",
                                 bpm=analysis.bpm if analysis else None):
            log.warning("postprocess: could not index CLAP vector for track %s", track_id)
            return False
        log.info("postprocess: embedded track %s with CLAP", track_id)
        label_clap_genre(track_id, vector, conn)
        return True
    except Exception:
        log.exception("postprocess: CLAP embedding failed for track %s", track_id)
        return False


#: Genre label embeddings, computed once per worker process.
_genre_labels: Optional[dict[str, list[float]]] = None


def label_clap_genre(track_id: int, vector, conn) -> Optional[str]:
    """Label the track's genre from its CLAP vector, as folder scans do.

    Without it a newly ingested song by an unclassified artist had no genre
    in any form until someone ran ``scripts/label_genres.py``. Best effort:
    a failure here never fails the embedding.
    """
    global _genre_labels
    try:
        from . import genre, localcache

        if _genre_labels is None:
            _genre_labels = genre.label_vectors()
        verdict = genre.classify(list(vector), _genre_labels)
        if verdict is None:
            return None
        localcache.record_genre(track_id, verdict, conn)
        return verdict.genre
    except Exception:
        log.warning("postprocess: CLAP genre label failed for track %s", track_id, exc_info=True)
        return None


def run_artist_logic(track_id: int, conn) -> str:
    """Link a newly ingested track to its MusicBrainz artists.

    A known artist string is linked instantly; a new one is resolved through
    this song (:func:`karaoke.artist_identity.resolve_track`), which also
    rewrites its genres. Artists stored for the first time then get their
    Wikidata facts and an artist-index document. Every part is best effort:
    MusicBrainz being busy leaves the string ``failed`` for the next
    ``make artists``, and never holds up the rest of the chain.
    """
    from . import artist_identity, musicbrainz

    try:
        status, fetched = artist_identity.resolve_track(track_id, conn, musicbrainz)
    except Exception:
        log.warning("postprocess: artist resolve failed for track %s", track_id, exc_info=True)
        return "error"
    if fetched:
        try:
            from . import artist_wikidata, wikidata

            artist_wikidata.run(conn, wikidata, mbids=fetched, out=lambda line: None)
            # Wikidata's genres stand in when MusicBrainz has no votes, so
            # the string's genres are rewritten once they are known.
            row = conn.execute("SELECT artist FROM tracks WHERE track_id = %s",
                               (track_id,)).fetchone()
            if row:
                artist_identity.derive_artist_genres(conn, artists=[row["artist"]])
        except Exception:
            log.warning("postprocess: Wikidata for %s failed", fetched, exc_info=True)
        try:
            from . import artist_index
            from .osclient import client

            artist_index.upsert(conn, client(), fetched)
        except Exception:
            log.warning("postprocess: artist index update for %s failed", fetched, exc_info=True)
    log.info("postprocess: artist for track %s: %s%s", track_id, status,
             f" ({len(fetched)} new)" if fetched else "")
    return status


def run_mood_logic(track_id: int, audio_path: Path, conn) -> bool:
    """Compute and store the track's mood timeline (sound + lyric meaning).

    On the CLAP worker: the timeline scores every ten-second window with the
    same model, which is only loaded there.
    """
    try:
        from . import mood_timeline

        timeline = mood_timeline.analyse_track(track_id, conn, audio_path=audio_path)
        if timeline is None:
            log.warning("postprocess: no mood timeline for track %s (%s)", track_id, audio_path)
            return False
        log.info("postprocess: mood timeline for track %s: %s", track_id,
                 timeline.summary.get("arc", ""))
        return True
    except Exception:
        log.exception("postprocess: mood timeline failed for track %s", track_id)
        return False
