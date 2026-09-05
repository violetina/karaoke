"""Serve one track out of a recording, for playback in the browser.

Record mode captures ten-minute segment files; a *track* is a span inside them
derived from the identification markers. Playing one back therefore means
cutting it out first, and this is where that cut is made and kept.

Playback itself is deliberately not implemented here. The project already plays
audio one way -- in the Chromium window that is open for YouTube Music and
Spotify -- and that window publishes MPRIS, which is where
:mod:`karaoke.playerctl` already reads position from. Serving a cut over HTTP
and opening it in that window therefore costs no new player, no second clock,
and no second set of sync offsets. A standalone player (mpv, ffplay, VLC) would
have introduced all three.

Two details that make the cache correct rather than merely fast:

- **The cut is named after its boundary, not its index.** Track 3 of a running
  recording is not a fixed thing: markers keep arriving, and a boundary can
  move. Including the start instant in the filename means a moved boundary
  misses the cache instead of silently serving the old audio.
- **Cuts live outside the capture directory.** ``prune_recordings`` measures a
  session by the size of its directory and deletes by age; derived files in
  there would inflate the first and be destroyed by the second.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .config import settings
from .logger import log

# Derived audio, safe to delete at any time. Kept beside the cache database
# rather than in the recording directory, which is accounted and pruned.
CACHE_DIRNAME = "recording-cuts"

# A cut is at most this long. A boundary derived from bad markers can claim an
# implausible span, and cutting an hour of audio to serve a "track" would be a
# slow way to fill the disk.
MAX_CUT_SECONDS = 20 * 60


def cache_dir() -> Path:
    """Where cut tracks are kept."""
    base = Path(settings.local_db).parent / CACHE_DIRNAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def cut_name(recording_id: int, index: int, start_wall: float) -> str:
    """Filename for one cut track.

    The start instant is part of the name so that a boundary which moves --
    which happens while a recording is still running and markers are still
    arriving -- does not hit a cut made from the previous boundary.
    """
    return f"rec{recording_id:03d}-{index:02d}-{start_wall:.0f}.flac"


def track_segment(recording_id: int, index: int, conn=None):
    """The segment at ``index`` in a recording's derived track list, or None."""
    from . import recorder, recording_slice

    marks = [m for m in recorder.load_marks(recording_id, conn) if m.ok]
    found = recording_slice.segments(marks)
    if index < 0 or index >= len(found):
        return None
    return found[index]


def track_audio(recording_id: int, index: int, conn=None) -> Optional[Path]:
    """Cut one track out of a recording and return the file, or None.

    Cached: cutting decodes and re-encodes the overlapping segments, so a track
    replayed or seeked around should not pay for it twice.
    """
    from . import recording_worker

    segment = track_segment(recording_id, index, conn)
    if segment is None:
        return None

    record = recording_worker.load_recording(recording_id, conn)
    if not record or not record["dir"]:
        return None
    directory = Path(record["dir"])
    if not directory.is_dir():
        log.debug("recording %s audio is gone from %s", recording_id, directory)
        return None

    dest = cache_dir() / cut_name(recording_id, index, segment.start_wall)
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    files = recording_worker.segment_files(directory)
    span = recording_worker.recording_span(files)
    window = recording_worker.clamp(segment, span) if span else None
    if window is None:
        log.debug("recording %s track %s falls outside the captured audio",
                  recording_id, index)
        return None

    start, end = window
    if end - start > MAX_CUT_SECONDS:
        log.warning("recording %s track %s claims %.0fs; capping the cut",
                    recording_id, index, end - start)
        end = start + MAX_CUT_SECONDS

    if not recording_worker.cut(files, start, end, dest):
        log.warning("could not cut recording %s track %s", recording_id, index)
        return None
    return dest


def browse_rows(recording_id: int, conn=None) -> list[dict]:
    """The playable track list for one recording, ready to render.

    Silent and unavailable tracks are included rather than filtered out. A gap
    in a session is evidence about it -- recording 12 stopped being audible at
    15:21 while capture ran to 15:36 -- and hiding those rows is how eight
    failed identifications looked like songrec being unreliable for an entire
    afternoon. They are marked, not omitted.
    """
    from . import localcache, recording_slice, recording_worker, silence

    own = conn is None
    conn = conn or localcache.connect()
    try:
        record = recording_worker.load_recording(recording_id, conn)
        if not record:
            return []
        directory = Path(record["dir"]) if record["dir"] else None
        files = (recording_worker.segment_files(directory)
                 if directory and directory.is_dir() else [])
        span = recording_worker.recording_span(files)
        gaps = silence.stored_map(recording_id, conn)

        from . import recorder

        marks = [m for m in recorder.load_marks(recording_id, conn) if m.ok]
        rows = []
        for index, segment in enumerate(recording_slice.segments(marks)):
            window = recording_worker.clamp(segment, span) if span else None
            rows.append({
                "index": index,
                "artist": segment.artist,
                "title": segment.title,
                "start_wall": segment.start_wall,
                "duration_s": segment.duration,
                "confident": recording_slice.is_confident(segment),
                "spread_s": (None if segment.spread == float("inf")
                             else segment.spread),
                "playable": window is not None,
                "silent": _mostly_silent(segment, files, gaps),
            })
        return rows
    finally:
        if own:
            conn.close()


def _mostly_silent(segment, files, gaps: dict) -> bool:
    """Whether a track's span is more silence than audio.

    Uses the stored map rather than re-detecting: the audio may already be
    pruned, and the map outlives it.
    """
    if not gaps:
        return False
    quiet = 0.0
    for seg_file in files:
        for gap in gaps.get(seg_file.path.name, []):
            start = max(segment.start_wall, seg_file.start_wall + gap.start)
            end = min(segment.end_wall, seg_file.start_wall + gap.end)
            if end > start:
                quiet += end - start
    return segment.duration > 0 and quiet > segment.duration / 2


def track_url(recording_id: int, index: int, *, port: Optional[int] = None) -> str:
    """The page that plays one recorded track.

    Points at the control API rather than the read-only one: cutting writes
    files and shells out to ffmpeg, which is host-side work by that module's
    own contract.
    """
    import os

    chosen = port or int(os.environ.get("KARAOKE_CTRL_PORT", "8765"))
    return f"http://localhost:{chosen}/recordings/{recording_id}/tracks/{index}"


def forget_cuts(recording_id: Optional[int] = None) -> int:
    """Delete cached cuts, for one recording or all. Returns the count.

    Derived data with an authoritative source, so this is always safe: the next
    request cuts again.
    """
    pattern = "rec*.flac" if recording_id is None else f"rec{recording_id:03d}-*.flac"
    removed = 0
    for path in cache_dir().glob(pattern):
        try:
            path.unlink()
            removed += 1
        except OSError:
            log.debug("could not remove cached cut %s", path, exc_info=True)
    return removed


def cached_bytes() -> int:
    """Total size of the cut cache."""
    return sum(f.stat().st_size for f in cache_dir().glob("rec*.flac")
               if f.is_file())


_CUT_NAME = re.compile(r"^rec(\d{3})-(\d{2})-(\d+)\.flac$")


def describe_cache() -> list[str]:
    """Human-readable listing of what has been cut, newest first."""
    rows = []
    for path in sorted(cache_dir().glob("rec*.flac"), reverse=True):
        match = _CUT_NAME.match(path.name)
        if not match:
            continue
        rows.append(f"  recording {int(match.group(1))} track "
                    f"{int(match.group(2))}  "
                    f"{path.stat().st_size / 1_000_000:6.1f} MB")
    return rows
