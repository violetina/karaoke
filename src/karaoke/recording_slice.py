"""Turn identification markers back into a track list.

Record mode captures the output continuously while asking songrec, every so
often, what is playing. Each answer is a *marker*: at wall-clock ``at_wall`` we
were ``at_offset`` seconds into some track. This module is the arithmetic that
turns a pile of those into "track X ran from A to B", which is what lets an
hours-long recording be cut back into songs and analysed offline.

The key move is that a marker does not merely name a track, it *dates* it::

    start_wall = at_wall - at_offset

Every marker of the same track therefore yields an independent estimate of when
that track began, and those estimates should agree. That is the entire basis for
trusting a boundary:

- **Agreement is confirmation.** Estimates clustering tightly mean the
  identification is solid and the boundary can be trusted.
- **Disagreement is a signal, not noise.** It means the track changed, repeated,
  was seeked, or songrec matched a different release. Those segments are
  reported with their spread and gated out of automatic analysis, because a key
  stored against the wrong track is worse than no key at all.

Everything here is a pure function over markers, so it is testable without any
audio — the same discipline as :mod:`karaoke.source_select` and
:mod:`karaoke.lyric_align`.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Iterable, Optional

# Two consecutive markers naming the same track are treated as one run unless
# they are further apart than this. Beyond it, the same song is more likely to
# have been played twice than to have run continuously with every intervening
# identification failing.
MAX_MARK_GAP_S = 240.0

# How far apart the per-marker start estimates may sit before the boundary is
# considered unreliable. songrec offsets are already median-clustered upstream
# by identify.robust_offset, so a few seconds is normal and a wide spread is
# genuinely suspicious.
MAX_SPREAD_S = 4.0

# One marker gives a start estimate with nothing to check it against. Segments
# below this are still reported -- they are exactly what a manual review wants
# to look at -- but they are not analysed automatically.
MIN_MARKS = 2


@dataclass(frozen=True)
class Mark:
    """One identification attempt against the recording timeline."""

    at_wall: float
    artist: str = ""
    title: str = ""
    at_offset: Optional[float] = None
    ok: bool = True

    @property
    def key(self) -> tuple[str, str]:
        """Case-insensitive identity of the named track."""
        return (self.artist.strip().casefold(), self.title.strip().casefold())

    @property
    def start_estimate(self) -> Optional[float]:
        """When the track began, per this marker alone."""
        if self.at_offset is None:
            return None
        return self.at_wall - self.at_offset


@dataclass(frozen=True)
class Segment:
    """A stretch of the recording believed to be one track."""

    artist: str
    title: str
    start_wall: float
    end_wall: float
    marks: int
    spread: float          # disagreement between start estimates, seconds

    @property
    def duration(self) -> float:
        return max(0.0, self.end_wall - self.start_wall)


def group_marks(marks: Iterable[Mark]) -> list[list[Mark]]:
    """Split markers into consecutive runs of the same track.

    A failed identification ends the current run rather than being skipped over.
    Silence, speech or an unrecognised track sitting between two matches of the
    same song is real evidence that they are two separate plays, and bridging
    the gap would merge them into one impossibly long segment.
    """
    runs: list[list[Mark]] = []
    current: list[Mark] = []

    def close_run() -> None:
        nonlocal current
        if current:
            runs.append(current)
            current = []

    for mark in sorted(marks, key=lambda m: m.at_wall):
        if not mark.ok or not mark.title.strip():
            # Ends the run; it does not discard it. What came before the gap
            # is still a real, complete stretch of that track.
            close_run()
            continue
        if current:
            same = current[-1].key == mark.key
            close = (mark.at_wall - current[-1].at_wall) <= MAX_MARK_GAP_S
            if same and close:
                current.append(mark)
                continue
            close_run()
        current = [mark]
    close_run()
    return runs


def segment_from(group: list[Mark], *, end_wall: Optional[float] = None) -> Segment:
    """Collapse one run of markers into a segment.

    The start is the **median** of the per-marker estimates, never the mean: a
    single bad match should not drag the boundary. ``identify.robust_offset``
    establishes the same idiom upstream for the same reason.
    """
    if not group:
        raise ValueError("cannot build a segment from no marks")

    estimates = [m.start_estimate for m in group if m.start_estimate is not None]
    if estimates:
        start = median(estimates)
        spread = max(estimates) - min(estimates)
    else:
        # No offsets at all: the best that can be said is that the track was
        # playing when it was first heard. Spread is infinite rather than zero
        # so this can never pass the confidence gate by accident.
        start = group[0].at_wall
        spread = float("inf")

    last = group[-1]
    if end_wall is None:
        # Without a following track, assume it ran at least to the last marker
        # plus whatever of it we had already heard.
        end_wall = last.at_wall + (last.at_offset or 0.0)
    return Segment(
        artist=group[0].artist,
        title=group[0].title,
        start_wall=start,
        end_wall=max(start, end_wall),
        marks=len(group),
        spread=spread,
    )


def segments(marks: Iterable[Mark]) -> list[Segment]:
    """Derive the full track list for a recording."""
    runs = group_marks(marks)
    out: list[Segment] = []
    for i, run in enumerate(runs):
        # A track ends where the next one starts, when there is a next one.
        following = None
        if i + 1 < len(runs):
            nxt = segment_from(runs[i + 1])
            following = nxt.start_wall
        out.append(segment_from(run, end_wall=following))
    return out


def is_confident(segment: Segment, *, max_spread: float = MAX_SPREAD_S,
                 min_marks: int = MIN_MARKS) -> bool:
    """Whether a segment's boundaries are trustworthy enough to analyse."""
    return (segment.marks >= min_marks
            and segment.spread <= max_spread
            and segment.duration > 0.0)


def describe(segment: Segment) -> str:
    """One-line human summary, for ``karaoke-recording --show``."""
    mins, secs = divmod(int(segment.duration), 60)
    flag = "ok " if is_confident(segment) else "?  "
    spread = "-" if segment.spread == float("inf") else f"{segment.spread:.1f}s"
    return (f"{flag} {segment.artist} - {segment.title}  "
            f"[{mins}:{secs:02d}, {segment.marks} marks, spread {spread}]")
