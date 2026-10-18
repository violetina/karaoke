"""When to ask songrec again: predict the song's end, and notice changes.

Radio mode (``karaoke -r``) and the TUI's mic mode used to identify on a fixed
clock -- every 30 s after the previous attempt, about 85 Shazam queries an
hour -- although after a match they already know where in the song they are,
and usually how long it is. Asking again mid-song only re-confirms the same
song; the moment that matters is when it ends.

So the next scan is *planned* from the matched position and the song's
length: one check a minute before the expected end -- a radio edit or a DJ
cutting a song short usually takes 30-60 s off -- and one just after it.
Two scans a song instead of five or six. A lightweight :class:`EndDetector`
listens alongside and asks for an early scan when the sound fades or stops,
which is how songs end. After a miss (talk, an ad, silence) the retries back
off instead of hammering.

Measured before building (a radio-style mix of library songs): comparing the
recent spectrum with the song's average -- plain bands, mel, MFCC or chroma
-- put only 2-3 of 10 song changes above a song's own verse/chorus contrast,
so a "the sound changed" detector was dropped. The level drop was the
opposite: over 90 minutes of whole songs it fired only in a song's last
1-7 s (its fade-out), and caught 14/14 pauses between songs.

Record mode is deliberately not touched: a recording's markers want a steady
rhythm, so :mod:`karaoke.recorder` keeps its fixed interval.
"""
from __future__ import annotations

import math
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from .logger import log

#: Scan this long after the song should have ended: the next one is playing
#: by then, and songrec needs a few seconds of it.
END_MARGIN_S = 6.0
#: Never sooner than this (a near-ending match) nor later than this (a long
#: track, or a wrong length): the cap bounds how long a missed change lasts.
MIN_WAIT_S = 20.0
MAX_WAIT_S = 300.0
#: Check once this long before the expected end, then again after it.
PRE_END_S = 60.0
#: Unknown length: half the old rate.
UNKNOWN_LENGTH_WAIT_S = 60.0
#: After consecutive misses: talk and ads last minutes, not seconds.
MISS_BACKOFF_S = (30.0, 60.0, 120.0, 180.0)


def next_wait(position_s: Optional[float], duration_s: Optional[float]) -> float:
    """Seconds until the next scan after a match.

    ``position_s`` is the playhead now (the same dead-reckoned position the
    lyrics use, lead included); ``duration_s`` the song's length if known.
    """
    if position_s is None or not duration_s or duration_s <= 0:
        return UNKNOWN_LENGTH_WAIT_S
    remaining = duration_s - position_s
    # Far from the end: look once a minute before it -- the match there either
    # re-confirms the song (and the next wait lands after the end) or finds
    # the shortened edit already over.
    if remaining > PRE_END_S + MIN_WAIT_S:
        wait = remaining - PRE_END_S
    else:
        wait = remaining + END_MARGIN_S
    return min(MAX_WAIT_S, max(MIN_WAIT_S, wait))


def miss_wait(consecutive_misses: int) -> float:
    """Seconds until the next scan after ``consecutive_misses`` (>= 1) misses."""
    i = max(0, min(consecutive_misses, len(MISS_BACKOFF_S)) - 1)
    return MISS_BACKOFF_S[i]


@dataclass
class ScanPlan:
    """The schedule for one listener: what happened, and when to look again."""

    misses: int = 0
    last_wait: float = 0.0
    reason: str = ""

    def after_match(self, position_s: Optional[float], duration_s: Optional[float]) -> float:
        self.misses = 0
        self.last_wait = next_wait(position_s, duration_s)
        self.reason = ("song end" if duration_s and position_s is not None
                       else "length unknown")
        return self.last_wait

    def after_miss(self) -> float:
        self.misses += 1
        self.last_wait = miss_wait(self.misses)
        self.reason = f"miss #{self.misses}"
        return self.last_wait


# -- end detection -----------------------------------------------------------

SAMPLE_RATE = 16000
FRAME_S = 0.5
#: The song's level is the median of its recent frames.
LEVEL_FRAMES = 40           # 20 s
#: This far below the song's own level, for this long, is a song ending or a
#: pause between songs. Measured on audio from disk; a room microphone has a
#: noise floor, so mic mode uses MIC_GAP_DB -- a guess, not a measurement.
GAP_DB = 30.0
MIC_GAP_DB = 18.0
GAP_FRAMES = 3              # 1.5 s
#: No two signals closer than this.
DEBOUNCE_S = 25.0
#: The first seconds of a song are its own baseline.
WARMUP_FRAMES = 12          # 6 s


@dataclass
class EndDetector:
    """Feed it audio frames; it says when a song has probably ended.

    One signal: the level dropping far below the song's own for a moment --
    a fade-out, or the pause before the next song. A false alarm costs one
    extra query; on 90 minutes of whole songs there were none.
    """

    gap_db: float = GAP_DB
    levels: deque = field(default_factory=lambda: deque(maxlen=LEVEL_FRAMES))
    gap_run: int = 0
    frames: int = 0
    last_signal: float = -1e9

    def reset(self) -> None:
        """A new song was matched: its level is the new baseline."""
        self.levels.clear()
        self.gap_run = self.frames = 0

    def feed(self, frame, now: Optional[float] = None) -> Optional[str]:
        """Add one frame; returns "end" when a scan is warranted."""
        import numpy as np

        now = time.monotonic() if now is None else now
        x = np.asarray(frame, dtype=np.float32)
        rms = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
        db = 20 * math.log10(max(rms, 1e-6))
        self.frames += 1
        signal = None
        if self.frames > WARMUP_FRAMES and len(self.levels) >= WARMUP_FRAMES:
            median_db = float(np.median(self.levels))
            self.gap_run = self.gap_run + 1 if db < median_db - self.gap_db else 0
            if self.gap_run >= GAP_FRAMES and now - self.last_signal >= DEBOUNCE_S:
                signal = "end"
        if self.gap_run == 0:
            # Quiet frames would drag the song's own level down.
            self.levels.append(db)
        if signal:
            self.last_signal = now
            self.gap_run = 0
        return signal


class ChangeWatcher:
    """Stream a source through an :class:`EndDetector` in the background.

    ``event`` is set when the song probably ended; the listener waits on it
    alongside its planned timeout. Uses ``parec`` like the live captions; if
    it is missing the watcher simply never fires and the plan alone decides.
    """

    def __init__(self, source: str, *, mic: bool = False) -> None:
        self.source = source
        self.detector = EndDetector(gap_db=MIC_GAP_DB if mic else GAP_DB)
        self.event = threading.Event()
        self.reason = ""
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> "ChangeWatcher":
        self._thread = threading.Thread(target=self._run, name="songwatch", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def song_matched(self) -> None:
        """Re-baseline on the newly identified song."""
        with self._lock:
            self.detector.reset()
            self.event.clear()

    def _run(self) -> None:
        import shutil

        import numpy as np

        if not shutil.which("parec") or not self.source:
            log.debug("songwatch: no parec or source; change detection off")
            return
        cmd = ["parec", "-d", self.source, f"--rate={SAMPLE_RATE}", "--channels=1",
               "--format=s16le", "--latency-msec=200"]
        chunk = int(SAMPLE_RATE * FRAME_S) * 2
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            log.debug("songwatch: parec failed to start", exc_info=True)
            return
        try:
            while not self._stop.is_set():
                raw = proc.stdout.read(chunk) if proc.stdout else b""
                if not raw or len(raw) < chunk:
                    if proc.poll() is not None:
                        return
                    continue
                frame = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                with self._lock:
                    signal = self.detector.feed(frame)
                if signal:
                    self.reason = signal
                    log.info("songwatch: song %s detected; scanning early", signal)
                    self.event.set()
        finally:
            try:
                proc.terminate()
            except OSError:
                pass


def wait_for_next_scan(stop: threading.Event, seconds: float,
                       watcher: Optional[ChangeWatcher] = None) -> str:
    """Sleep until the planned scan, an early change signal, or stop.

    Returns "stop", "end" (the watcher fired) or "planned".
    """
    deadline = time.monotonic() + seconds
    while not stop.is_set():
        left = deadline - time.monotonic()
        if left <= 0:
            return "planned"
        if watcher is not None and watcher.event.is_set():
            watcher.event.clear()
            return watcher.reason or "end"
        stop.wait(min(1.0, left))
    return "stop"
