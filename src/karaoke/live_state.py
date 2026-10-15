"""Where a live-listening mode is in the song, for views in other processes.

Radio mode (``karaoke -r``, and the TUI's mic mode) has no player to ask: it
identifies the song with songrec and dead-reckons the playhead from there.
That state lived only inside the listening process, so the stage views served
by ctrl-api -- ``/mood`` among them -- showed "Stopped" while a song played.

Each listener publishes a small JSON file here, rewritten on every change and
on a heartbeat. Readers trust it only while the heartbeat is fresh and the
publishing process is alive, so a crashed listener cannot leave a song
"playing" forever.

The position is published as an anchor, not a number: ``offset`` seconds into
the track at wall-clock ``anchor_wall``, plus the lead and the user's nudge.
Readers reckon forward from it with their own clock, exactly as the listener
does with its monotonic one.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from .logger import log

#: A state whose heartbeat is older than this is from a listener that stopped
#: without clearing it -- killed, or suspended with the machine.
STALE_AFTER_S = 10.0

#: How often a listener should refresh an unchanged state.
HEARTBEAT_S = 2.0

#: Publisher names, one file each.
SOURCES = ("radio", "tui-radio")


def state_dir() -> Path:
    from .config import settings

    return Path(settings.data_dir) / "live"


def _path(source: str) -> Path:
    return state_dir() / f"{source}.json"


def anchor_wall(offset_mono: float, *, now_wall: Optional[float] = None,
                now_mono: Optional[float] = None) -> float:
    """The wall-clock instant a monotonic timestamp refers to."""
    now_wall = time.time() if now_wall is None else now_wall
    now_mono = time.monotonic() if now_mono is None else now_mono
    return now_wall - (now_mono - offset_mono)


def publish(source: str, *, artist: str, title: str,
            offset: Optional[float], offset_mono: Optional[float],
            lead: float = 0.0, nudge: float = 0.0, status: str = "",
            session_id: Optional[int] = None,
            recording_id: Optional[int] = None) -> None:
    """Write the listener's current song and clock anchor. Never raises."""
    data: dict[str, Any] = {
        "source": source,
        "pid": os.getpid(),
        "artist": artist,
        "title": title,
        "offset": offset,
        "anchor_wall": anchor_wall(offset_mono) if offset_mono is not None else None,
        "lead": lead,
        "nudge": nudge,
        "status": status,
        "session_id": session_id,
        "recording_id": recording_id,
        "heartbeat": time.time(),
    }
    try:
        directory = state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / f".{source}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(data))
        os.replace(tmp, _path(source))      # readers never see half a file
    except OSError:
        log.debug("could not publish live state for %s", source, exc_info=True)


def clear(source: str) -> None:
    """Remove a listener's state, but only if this process wrote it."""
    try:
        path = _path(source)
        data = json.loads(path.read_text())
        if data.get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass


def _alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True                     # exists, owned by someone else
    return True


def read(source: str, *, now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """A listener's state if it is current, else None."""
    now = time.time() if now is None else now
    try:
        data = json.loads(_path(source).read_text())
    except (OSError, ValueError):
        return None
    if now - float(data.get("heartbeat") or 0.0) > STALE_AFTER_S:
        return None
    if not _alive(data.get("pid")):
        return None
    return data


def position(state: dict[str, Any], *, now: Optional[float] = None) -> Optional[float]:
    """Seconds into the track right now, or None before the first match."""
    if state.get("offset") is None or state.get("anchor_wall") is None:
        return None
    now = time.time() if now is None else now
    pos = (float(state["offset"]) + (now - float(state["anchor_wall"]))
           + float(state.get("lead") or 0.0) + float(state.get("nudge") or 0.0))
    return max(0.0, pos)


def current(*, now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """The freshest live listener with a song, if any."""
    states = [s for s in (read(src, now=now) for src in SOURCES)
              if s and s.get("title")]
    return max(states, key=lambda s: s["heartbeat"], default=None)


def recording_playhead(conn, *, now: Optional[float] = None,
                       max_age_s: float = 120.0) -> Optional[dict[str, Any]]:
    """Song and position from the latest mark of a running recording.

    Record mode identifies the output on a loop and stores each answer as a
    mark. The mark's wall time is when the answer came back, after songrec's
    window, so the position is corrected by the same measured lead
    :mod:`karaoke.recording_slice` uses to date track starts.
    """
    from .recording_slice import RECOGNITION_LEAD_S

    now = time.time() if now is None else now
    row = conn.execute(
        """
        SELECT m.recording_id, m.at_wall, m.at_offset, m.artist, m.title
        FROM recording_marks m
        JOIN recordings r ON r.recording_id = m.recording_id
        WHERE r.status = 'recording' AND m.ok = 1 AND m.at_offset IS NOT NULL
        ORDER BY m.at_wall DESC
        LIMIT 1
        """
    ).fetchone()
    # A mark older than a couple of identification cycles may describe a song
    # that has since ended; the next mark would have replaced it by now.
    if row is None or now - float(row["at_wall"]) > max_age_s:
        return None
    return {
        "source": "recording",
        "recording_id": int(row["recording_id"]),
        "artist": row["artist"],
        "title": row["title"],
        "position_s": max(0.0, float(row["at_offset"]) + (now - float(row["at_wall"]))
                          + RECOGNITION_LEAD_S),
    }
