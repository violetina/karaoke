"""Beat detection (local audio) + pure flash-timing helpers.

`detect_beats` needs librosa (heavy: numba/llvmlite) so it's imported lazily and
only used in --file mode where we have the actual audio. Everything else here is
pure and unit-tested: `beat_on` decides whether the display should be "flashed"
at a given elapsed time, and `line_pulse` is the fallback used by modes with no
audio (Spotify/live) — it flashes briefly at the start of each lyric line.
"""
from __future__ import annotations

import bisect
from typing import Optional, Sequence


def detect_beats(audio_path: str) -> tuple[float, list[float]]:
    """Return (tempo_bpm, beat_times_seconds) for a local audio file.

    Uses librosa's beat tracker. Returns (0.0, []) if librosa is unavailable or
    the file can't be analysed — callers must degrade gracefully (no flashing).
    """
    try:
        import librosa  # lazy: pulls numba/llvmlite, only wanted for --file
    except Exception:
        return (0.0, [])
    try:
        y, sr = librosa.load(audio_path, mono=True)
        tempo, frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
        times = librosa.frames_to_time(frames, sr=sr)
        bpm = float(tempo) if not hasattr(tempo, "__len__") else float(tempo[0])
        return (bpm, [float(t) for t in times])
    except Exception:
        return (0.0, [])


def beat_on(beat_times: Sequence[float], elapsed: float, hold: float = 0.11) -> bool:
    """True if `elapsed` is within `hold` seconds AFTER the most recent beat.

    Gives a short on-beat flash. Pure: `beat_times` must be sorted ascending
    (librosa returns them sorted). Returns False before the first beat or when
    there are no beats.
    """
    if not beat_times or elapsed < beat_times[0]:
        return False
    i = bisect.bisect_right(beat_times, elapsed) - 1
    if i < 0:
        return False
    return (elapsed - beat_times[i]) <= hold


def nearest_bpm_hold(bpm: float, fraction: float = 0.25, cap: float = 0.18) -> float:
    """A sensible flash `hold` from tempo: a fraction of the beat period, capped.

    Faster songs -> shorter flashes so they don't blur together. Falls back to
    `cap` when bpm is unknown/nonpositive.
    """
    if bpm <= 0:
        return cap
    period = 60.0 / bpm
    return min(cap, max(0.05, period * fraction))


def line_pulse(line_start: Optional[float], elapsed: float, hold: float = 0.18) -> bool:
    """Fallback flash for audio-less modes: pulse at the start of a lyric line.

    `line_start` is the active line's timestamp (None in the intro). True for the
    first `hold` seconds after a line becomes active, so the border blinks once
    per line even without real beat data.
    """
    if line_start is None:
        return False
    return 0.0 <= (elapsed - line_start) <= hold
