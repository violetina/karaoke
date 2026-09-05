"""Score the lyric aligner against tracks whose real timings we already have.

Every tuning decision in :mod:`karaoke.lyric_align` so far was made by ear on
one Dinosaur Jr. track. That is how ``BREAK_BARS`` came to be changed from 4 to
8 with the only honest report being *"changed nothing"* -- there was no way to
see what it moved.

This makes it measurable. While recording in Spotify mode, many tracks already
have approved **synced** lyrics, and those timings are ground truth. Run the
transcribe-and-align chain over a recording of such a track and the difference
between the two is a number.

Three things it has to be careful about, or the number is worthless:

- **The recording clock is not the track clock.** ``Segment.start_wall`` is the
  *median* estimate of when the track began, from songrec offsets, so cut audio
  starts at the track's own zero -- give or take the segment's ``spread``.
- **That boundary error is systematic**, and it is not the aligner's fault. A
  segment start 2 s late shifts every line by 2 s. So the signed median is
  reported separately from the jitter around it: the median absorbs the
  boundary, and the spread around the median is what the aligner actually
  contributes.
- **A skipped track is captured in part.** Judging it on lines that never
  played would report failure for words that were never sung. The stored
  silence map bounds the span, and only ground-truth lines inside it are
  scored.

The report says how many tracks it could score. It can only use tracks that
already have synced lyrics, so it measures the aligner on the case where a real
source exists and infers about the case where none does -- which is the case
that actually needs it. That limit is printed, not glossed.

Usage:
    python -m scripts.eval_align 13 [--model small] [--limit 5]
"""
from __future__ import annotations

import argparse
import statistics
import tempfile
import time
from pathlib import Path
from typing import Optional

from karaoke import (localcache, lyric_align, recorder, recording_slice as rs,
                     recording_worker, silence, whisper_sync)
from karaoke.logger import log
from karaoke.lyrics import parse_lrc

# "Would a singer notice." Below a quarter second a line reads as on time; past
# it the words arrive visibly off the vocal.
NOTICEABLE_S = 0.3


def ground_truth(track_id: int, conn) -> list[tuple[float, str]]:
    """The approved synced lyrics for a track, parsed, or an empty list."""
    row = conn.execute(
        """
        SELECT synced_lyrics FROM lyrics
        WHERE track_id = ? AND kind = 'approved'
          AND synced_lyrics IS NOT NULL AND synced_lyrics != ''
        ORDER BY lyric_id DESC LIMIT 1
        """,
        (track_id,),
    ).fetchone()
    return parse_lrc(row["synced_lyrics"]) if row else []


def track_bpm(track_id: int, conn) -> Optional[float]:
    """Analysed tempo, if the sound analysis has run for this track."""
    try:
        row = conn.execute(
            "SELECT bpm FROM track_analysis WHERE track_id = ?", (track_id,)
        ).fetchone()
    except Exception:
        return None
    return row["bpm"] if row and row["bpm"] else None


def audible_end(segment: rs.Segment, files: list, stored: dict) -> float:
    """Where this track's audio actually stops, in seconds from its start."""
    end = segment.end_wall
    for seg_file in files:
        for gap in stored.get(seg_file.path.name, []):
            gap_start = seg_file.start_wall + gap.start
            gap_end = seg_file.start_wall + gap.end
            if (gap_start <= end and gap_end >= end - 1.0
                    and gap_start > segment.start_wall):
                end = gap_start
    return max(0.0, end - segment.start_wall)


def score(produced: list[tuple[float, str]],
          truth: list[tuple[float, str]],
          horizon: float) -> Optional[dict]:
    """Compare produced timings against ground truth, line for line.

    Lines are paired by position after both are limited to the audible span,
    which is what makes a partial capture scorable rather than a failure.
    """
    truth_in_span = [(t, text) for t, text in truth if t <= horizon]
    if not truth_in_span or not produced:
        return None

    pairs = list(zip(produced, truth_in_span))
    if not pairs:
        return None

    signed = [p[0] - t[0] for p, t in pairs]
    absolute = [abs(d) for d in signed]
    median_signed = statistics.median(signed)
    # Error with the systematic part removed: the segment boundary shifts every
    # line equally, and that is not something the aligner did.
    jitter = [abs(d - median_signed) for d in signed]

    ordered = sorted(absolute)
    p90 = ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))]
    return {
        "lines": len(pairs),
        "truth_lines": len(truth),
        "scored_of": len(truth_in_span),
        "median_abs": statistics.median(absolute),
        "median_signed": median_signed,
        "median_jitter": statistics.median(jitter),
        "p90_abs": p90,
        "within": sum(1 for d in absolute if d <= NOTICEABLE_S) / len(absolute),
        "within_detrended": sum(1 for d in jitter if d <= NOTICEABLE_S) / len(jitter),
    }


def evaluate(recording_id: int, *, model_size: str = "small",
             limit: Optional[int] = None) -> int:
    conn = localcache.connect()
    record = recording_worker.load_recording(recording_id, conn)
    if not record:
        print(f"no recording {recording_id}")
        return 1
    directory = Path(record["dir"])
    if not directory.is_dir():
        print(f"recording {recording_id} has no audio left at {directory}")
        return 1

    files = recording_worker.segment_files(directory)
    stored = silence.stored_map(recording_id, conn)
    marks = [m for m in recorder.load_marks(recording_id, conn) if m.ok]
    segments = rs.segments(marks)

    scorable, skipped_no_lrc = [], 0
    for seg in segments:
        track_id = localcache.find_track_id(seg.artist, seg.title, conn)
        if track_id is None:
            skipped_no_lrc += 1
            continue
        truth = ground_truth(track_id, conn)
        if not truth:
            skipped_no_lrc += 1
            continue
        scorable.append((seg, track_id, truth))

    print(f"recording {recording_id}: {len(segments)} track(s), "
          f"{len(scorable)} with synced lyrics to score against "
          f"({skipped_no_lrc} without)\n")
    if not scorable:
        print("nothing to score. Play some songs that already have lyrics —\n"
              "the aligner can only be measured where the truth is known.")
        return 0

    if limit:
        scorable = scorable[:limit]

    results = []
    for seg, track_id, truth in scorable:
        horizon = audible_end(seg, files, stored)
        print(f"{seg.artist} - {seg.title}")
        print(f"  {len(truth)} true line(s), audible for {horizon / 60:.1f} min, "
              f"boundary spread {seg.spread:.1f}s")

        if horizon < 20:
            print("  too little audio; skipped\n")
            continue

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "cut.flac"
            if not recording_worker.cut(files, seg.start_wall,
                                        seg.start_wall + horizon, audio):
                print("  could not cut audio; skipped\n")
                continue
            t0 = time.time()
            try:
                words = whisper_sync.transcribe_to_words(audio, model_size=model_size)
            except Exception as exc:
                print(f"  transcription failed: {exc}\n")
                continue

        plain = "\n".join(text for _t, text in truth)
        produced = parse_lrc(lyric_align.align_lyrics_to_lrc(
            plain, words, total_duration=horizon,
            bpm=track_bpm(track_id, conn)))

        stats = score(produced, truth, horizon)
        if stats is None:
            print(f"  nothing to compare ({len(words)} words, "
                  f"{time.time() - t0:.0f}s)\n")
            continue

        stats["label"] = f"{seg.artist} - {seg.title}"
        stats["spread"] = seg.spread
        results.append(stats)
        print(f"  scored {stats['lines']} line(s) of "
              f"{stats['scored_of']} in span ({len(words)} whisper words, "
              f"{time.time() - t0:.0f}s)")
        print(f"    median |error|   {stats['median_abs']:6.2f}s")
        print(f"    median signed    {stats['median_signed']:+6.2f}s "
              "(systematic — segment boundary, not the aligner)")
        print(f"    median jitter    {stats['median_jitter']:6.2f}s "
              "(the aligner's own contribution)")
        print(f"    p90 |error|      {stats['p90_abs']:6.2f}s")
        print(f"    within {NOTICEABLE_S}s      "
              f"{stats['within'] * 100:5.1f}%  "
              f"({stats['within_detrended'] * 100:.1f}% detrended)\n")

    if not results:
        print("no track could be scored")
        return 0

    print("=" * 62)
    print(f"{len(results)} track(s) scored")
    print(f"  median |error| across tracks   "
          f"{statistics.median([r['median_abs'] for r in results]):6.2f}s")
    print(f"  median jitter across tracks    "
          f"{statistics.median([r['median_jitter'] for r in results]):6.2f}s")
    print(f"  worst p90                      "
          f"{max(r['p90_abs'] for r in results):6.2f}s  "
          f"({max(results, key=lambda r: r['p90_abs'])['label']})")
    print(f"  mean within {NOTICEABLE_S}s           "
          f"{statistics.fmean([r['within'] for r in results]) * 100:5.1f}%")
    print()
    print("Measured only on tracks that already have synced lyrics, which is")
    print("not the case the aligner exists for. Read it as an upper bound.")
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("recording_id", type=int)
    ap.add_argument("--model", default="small")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)
    log.info("evaluating alignment on recording %s", args.recording_id)
    return evaluate(args.recording_id, model_size=args.model, limit=args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
