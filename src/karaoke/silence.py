"""Find the silent stretches in a recording.

A capture is not all music. Playback stops — YouTube Music pauses a device when
the account starts playing somewhere else, and that happened mid-album here:
identification succeeded until 15:21 and failed on every attempt afterwards,
because there was nothing to hear. Twenty-eight minutes of the session were
music and eight were nothing.

Silence is worth finding for three reasons:

- it explains a run of failed identifications, which otherwise look like
  songrec being unreliable;
- it bounds the audio that is worth transcribing or analysing, and Whisper on
  silence is exactly where it invents text;
- it is the natural place to cut, since a gap between tracks is silence and so
  is the end of the session.

Detection is delegated to ffmpeg's ``silencedetect`` filter rather than
computing RMS here: it is already a dependency, it is well tested, and it
reports in the same wall-clock terms the recording is stored in.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .logger import log

# Quieter than this counts as silence. -50dB is below the noise floor of a
# monitor capture but above true digital zero, which a paused stream is not:
# there is usually a faint hiss rather than nothing at all.
DEFAULT_THRESHOLD_DB = -50.0

# Shorter gaps than this are the space between tracks or a breath in the music,
# not a stop. Two seconds keeps ordinary album gaps out of the results.
DEFAULT_MIN_SECONDS = 2.0

_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


@dataclass(frozen=True)
class Silence:
    """One silent stretch, in seconds from the start of the file."""

    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def detect(path: Path, *, threshold_db: float = DEFAULT_THRESHOLD_DB,
           min_seconds: float = DEFAULT_MIN_SECONDS,
           timeout: float = 600.0) -> list[Silence]:
    """Silent stretches in one audio file, in order.

    Returns an empty list when the file cannot be read: absence of evidence is
    reported as no silence found, never as silence, since the caller may be
    about to delete or skip whatever this covers.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-i", str(path),
        "-af", f"silencedetect=noise={threshold_db}dB:d={min_seconds}",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("silencedetect failed for %s: %s", path, exc)
        return []

    # silencedetect writes one "silence_start" then one "silence_end" per
    # stretch, interleaved with progress output, so they are paired in order.
    starts = [float(m.group(1)) for m in _START.finditer(proc.stderr or "")]
    ends = [float(m.group(1)) for m in _END.finditer(proc.stderr or "")]
    out = [Silence(start=s, end=e) for s, e in zip(starts, ends) if e > s]

    # A stretch still open when the file ends has no silence_end. That is the
    # common case for a session left running after playback stopped, and it is
    # the most interesting one, so it is closed at the file's own duration.
    if len(starts) > len(ends):
        total = measured_duration(path)
        if total and total > starts[-1]:
            out.append(Silence(start=starts[-1], end=total))
    return out


def measured_duration(path: Path) -> Optional[float]:
    """Length of a captured segment, by decoding it.

    Not ``coverart.duration``: the segment muxer writes FLAC without a duration
    header, so a finished segment reports N/A and the final one reports the
    *whole session* -- which produced silence percentages of 243% and 50361%
    before this was measured properly instead of read.
    """
    from .recording_worker import decoded_duration

    return decoded_duration(path)


def total_silence(stretches: list[Silence]) -> float:
    return sum(s.duration for s in stretches)


def loudest_span(stretches: list[Silence], total: float) -> Optional[tuple[float, float]]:
    """The longest stretch that is *not* silent, or None.

    What to transcribe or analyse when a session is part music and part
    nothing: the single longest run of audio between the gaps.
    """
    if total <= 0:
        return None
    if not stretches:
        return (0.0, total)
    best: Optional[tuple[float, float]] = None
    cursor = 0.0
    for gap in sorted(stretches, key=lambda s: s.start):
        if gap.start > cursor:
            span = (cursor, min(gap.start, total))
            if best is None or (span[1] - span[0]) > (best[1] - best[0]):
                best = span
        cursor = max(cursor, gap.end)
    if cursor < total:
        span = (cursor, total)
        if best is None or (span[1] - span[0]) > (best[1] - best[0]):
            best = span
    return best


def scan(recording_id: int, *, conn=None, rescan: bool = False,
         skip_last: bool = True) -> dict[str, list[Silence]]:
    """Detect and store the silence map for a recording, per segment file.

    Already-scanned files are skipped unless ``rescan`` -- not because
    detection is slow (36 minutes of audio takes about a second) but because
    the stored map outlives the audio, which is deleted after a week.

    ``skip_last`` leaves the newest segment alone: while a recording is running
    that file is still being written, so its silence map would be a snapshot of
    an incomplete file and its measured duration would be wrong. Recording 13
    is live as this lands, and scanning it must not require stopping it.
    """
    from . import localcache
    from .recorder import session_directory
    from .recording_worker import load_recording, segment_files

    own = conn is None
    conn = conn or localcache.connect()
    try:
        record = load_recording(recording_id, conn)
        directory = Path(record["dir"]) if record and record["dir"] else \
            session_directory(recording_id)
        if not directory or not Path(directory).is_dir():
            log.debug("recording %s has no audio directory", recording_id)
            return {}

        files = segment_files(Path(directory))
        if skip_last and files:
            files = files[:-1]
        done = localcache.silence_scans(recording_id, conn)

        found: dict[str, list[Silence]] = {}
        for seg in files:
            name = seg.path.name
            if name in done and not rescan:
                continue
            stretches = detect(seg.path)
            localcache.record_silence(
                recording_id, name, [(s.start, s.end) for s in stretches],
                conn, duration_s=measured_duration(seg.path))
            found[name] = stretches
            log.info("recording %s %s: %d silent stretch(es), %.0fs quiet",
                     recording_id, name, len(stretches),
                     total_silence(stretches))
        return found
    finally:
        if own:
            conn.close()


def stored_map(recording_id: int, conn) -> dict[str, list[Silence]]:
    """The silence map as stored, per segment file name."""
    from . import localcache

    out: dict[str, list[Silence]] = {}
    for row in localcache.silence_for_recording(recording_id, conn):
        out.setdefault(row["file"], []).append(
            Silence(start=row["start_s"], end=row["end_s"]))
    return out


def audible_until(recording_id: int, conn) -> Optional[float]:
    """Wall-clock instant after which the recording holds nothing but silence.

    This is where a capture *effectively* ended, which is not where it stopped:
    recording 12 ran for 36 minutes and its last 6 were entirely quiet because
    YouTube Music had moved playback to another device.

    It matters for scoring alignment on a skipped track. A song skipped part
    way through is captured in part, and the aligner must be judged on the
    lines that actually played rather than penalised for the ones that never
    did.

    None means nothing in the recording was ever audible, which is a different
    claim from "the audio ended when capture began" -- a caller must handle it
    rather than be handed a zero-length span that looks like a real one.
    """
    from .recorder import session_directory
    from .recording_worker import load_recording, segment_files

    record = load_recording(recording_id, conn)
    directory = Path(record["dir"]) if record and record["dir"] else \
        session_directory(recording_id)
    if not directory or not Path(directory).is_dir():
        return None

    stored = stored_map(recording_id, conn)
    latest: Optional[float] = None
    for seg in segment_files(Path(directory)):
        gaps = stored.get(seg.path.name, [])
        span = loudest_span(gaps, seg.duration)
        if span is None:
            continue
        # A segment that is silent to its own end contributes the instant the
        # audio stopped; one that ends mid-music contributes its end.
        trailing = any(g.end >= seg.duration - 1.0 for g in gaps)
        end_offset = span[1] if trailing else seg.duration
        candidate = seg.start_wall + end_offset
        if latest is None or candidate > latest:
            latest = candidate
    return latest


def describe(stretches: list[Silence]) -> list[str]:
    """Human-readable rows, for a CLI or the TUI."""
    out = []
    for gap in stretches:
        mins, secs = divmod(int(gap.start), 60)
        length = gap.duration
        out.append(f"  {mins}:{secs:02d} + {length:5.1f}s silent")
    return out
