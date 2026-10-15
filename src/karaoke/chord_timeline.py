"""A track's chords over time, stored so playback views can show them.

The progression index keeps what search needs -- a motion histogram and a few
scalars -- and nothing with a timestamp, so the chord under the playhead could
not be shown anywhere. This keeps the sequence itself, one row per track,
written wherever chords are detected and read by the stage stream.

A missing timeline is filled on demand for the track that is playing, from
audio already on disk, in a background thread: chord detection takes a few
seconds, and the stage stream ticks ten times a second.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Optional

from psycopg.types.json import Jsonb

from .logger import log

_SCHEMA = """
CREATE TABLE IF NOT EXISTS track_chords (
    track_id       INTEGER PRIMARY KEY,
    chords_version INTEGER NOT NULL,
    duration_s     REAL,
    chords         JSONB NOT NULL,
    harmony        JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_path    TEXT NOT NULL DEFAULT '',
    updated_at     DOUBLE PRECISION NOT NULL
)
"""

#: A playing track whose length differs from the analysed file by more than
#: this is a different edit -- typically the video with an intro the audio
#: release lacks -- and chords placed by the file's clock would be wrong.
LENGTH_TOLERANCE_S = 5.0
LENGTH_TOLERANCE_RATIO = 0.05


def ensure_schema(conn) -> None:
    conn.execute(_SCHEMA)
    conn.commit()


def save(track_id: int, analysis: Any, conn, *, source_path: str = "",
         key_text: str = "") -> None:
    """Store a ChordAnalysis's sequence and its harmony summary."""
    from . import chords, harmony

    key = harmony.key_from_text(key_text, analysis) if key_text else None
    summary = harmony.summarise(analysis, key)
    key = key or (harmony.Key(summary.key_tonic, summary.key_minor) if summary else None)
    rows = [{
        "at": round(c.at_s, 2),
        "end": round(c.end_s, 2),
        "name": c.name,
        "roman": harmony.roman(c, key) if key else "",
    } for c in analysis.chords]
    ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO track_chords
            (track_id, chords_version, duration_s, chords, harmony, source_path, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (track_id) DO UPDATE SET
            chords_version = excluded.chords_version,
            duration_s = excluded.duration_s,
            chords = excluded.chords,
            harmony = excluded.harmony,
            source_path = excluded.source_path,
            updated_at = excluded.updated_at
        """,
        (track_id, chords.CHORDS_VERSION, round(analysis.duration_s, 2), Jsonb(rows),
         Jsonb(summary.as_doc() if summary else {}), source_path, time.time()),
    )
    conn.commit()


def load(track_id: int, conn) -> Optional[dict[str, Any]]:
    """The stored timeline for a track, or None."""
    ensure_schema(conn)
    row = conn.execute(
        "SELECT chords_version, duration_s, chords, harmony FROM track_chords"
        " WHERE track_id = %s", (track_id,)).fetchone()
    return dict(row) if row else None


def at(timeline: dict[str, Any], position_s: float) -> dict[str, Any]:
    """The chord sounding at a position and the one after it.

    Returns ``chord``/``chord_roman`` (None between chords) and
    ``next_chord``/``next_chord_in``.
    """
    out: dict[str, Any] = {"chord": None, "chord_roman": None,
                           "next_chord": None, "next_chord_in": None}
    for row in timeline.get("chords") or []:
        if row["at"] <= position_s < row["end"]:
            out["chord"], out["chord_roman"] = row["name"], row.get("roman") or None
        elif row["at"] > position_s:
            out["next_chord"] = row["name"]
            out["next_chord_in"] = round(row["at"] - position_s, 1)
            break
    return out


def matches_playback(timeline: dict[str, Any], playing_duration: float) -> bool:
    """Whether the analysed file and what is playing are the same edit."""
    analysed = timeline.get("duration_s") or 0.0
    if not analysed or not playing_duration:
        return True              # nothing to compare against; trust it
    tolerance = max(LENGTH_TOLERANCE_S, LENGTH_TOLERANCE_RATIO * analysed)
    return abs(analysed - playing_duration) <= tolerance


#: A file shorter than this, or than this share of the track, is a clip --
#: ~/Music/karaoke-samples holds one-second captures filed as local sources
#: of the songs they sampled -- and analysing it describes one second.
MIN_AUDIO_S = 30.0
MIN_AUDIO_SHARE = 0.5


def _audio_seconds(path: Path) -> Optional[float]:
    """Length from the file's header, or None when it cannot be read."""
    try:
        from mutagen import File as MutagenFile

        info = getattr(MutagenFile(str(path)), "info", None)
        return float(info.length) if info and info.length else None
    except Exception:
        return None


def _whole_song(path: Path, track_seconds: Optional[float]) -> bool:
    seconds = _audio_seconds(path)
    if seconds is None:
        return True                      # unreadable header: let the decoder decide
    floor = max(MIN_AUDIO_S, MIN_AUDIO_SHARE * (track_seconds or 0.0))
    return seconds >= floor


def local_audio(track_id: int, conn) -> Optional[Path]:
    """A file on disk holding this track's whole audio, if there is one."""
    from . import localcache
    from .config import settings

    row = conn.execute("SELECT duration FROM tracks WHERE track_id = %s",
                       (track_id,)).fetchone()
    track_seconds = float(row["duration"]) if row and row["duration"] else None
    for row in conn.execute("SELECT kind, url FROM sources WHERE track_id = %s",
                            (track_id,)).fetchall():
        url = row["url"] or ""
        candidates: list[Path] = []
        # radio_capture: a song sliced out of a radio recording, on disk.
        if row["kind"] in ("local", "radio_capture"):
            candidates = [Path(url.removeprefix("file://"))]
        else:
            vid = localcache.extract_youtube_id(url)
            if vid:
                candidates = [p for p in sorted(Path(settings.youtube_dir).glob(f"{vid}.*"))
                              if not p.name.endswith((".part", ".json"))]
        for path in candidates:
            if path.is_file() and _whole_song(path, track_seconds):
                return path
    return None


_pending: set[int] = set()
_pending_lock = threading.Lock()


def fill_in_background(track_id: int) -> bool:
    """Detect and store chords for a track off the caller's thread.

    Returns False when a fill for this track is already running. A track with
    no audio on disk is remembered as attempted, so the stage stream does not
    retry it ten times a second; restarting the process clears that.
    """
    with _pending_lock:
        if track_id in _pending:
            return False
        _pending.add(track_id)

    def work() -> None:
        from . import chords, localcache

        try:
            with localcache.connect() as conn:
                path = local_audio(track_id, conn)
                if path is None:
                    log.debug("chord timeline: no audio on disk for track %s", track_id)
                    return
                analysis = chords.detect(str(path))
                if analysis is None:
                    return
                row = conn.execute(
                    "SELECT detected_key FROM track_analysis WHERE track_id = %s",
                    (track_id,)).fetchone()
                save(track_id, analysis, conn, source_path=str(path),
                     key_text=(row["detected_key"] if row else "") or "")
                log.info("chord timeline: stored %d chords for track %s",
                         len(analysis.chords), track_id)
                with _pending_lock:
                    _pending.discard(track_id)   # done: a later miss may retry
        except Exception:
            log.debug("chord timeline: fill failed for track %s", track_id, exc_info=True)

    threading.Thread(target=work, name=f"chords-{track_id}", daemon=True).start()
    return True
