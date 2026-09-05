"""Transcribe the tracks in a recording, and keep the result as notes.

This exists for the case no other source can serve. The Wizards of Ooze album
in recording 12 has no lyrics anywhere: LRCLIB, Genius, the YouTube Music panel
and YouTube captions all came back empty, and there are not even auto-captions.
Whisper is the only route to words, which makes it the text rather than a
fallback for timing it.

Three things make this worth running rather than guessing at:

- **Silence bounds the work.** The stored map says where each track's audio
  really is, and Whisper on silence is precisely where it invents text. The
  last track here was cut off after 30 seconds when playback moved to another
  device; transcribing the following quarter of an hour of nothing would
  produce confident nonsense.
- **The output is a note first.** It goes to ``track_notes`` as a
  transcription with its mean word confidence, always. It is promoted into the
  lyrics table only for a track that has no lyrics at all, and then with
  ``source`` saying ``whisper`` plainly so it is never mistaken for real words.
- **Confidence is recorded, not discarded.** ``Word.probability`` is what
  distinguishes a misheard line from a sung one, and it is the number that
  decides whether this album's text is worth anything.

Usage:
    python -m scripts.process_recording 12 [--model small] [--dry-run]
"""
from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from karaoke import (localcache, recorder, recording_slice as rs,
                     recording_worker, silence, whisper_clean, whisper_sync)
from karaoke.logger import log
from karaoke.lyrics import Lyrics

# Below this mean word confidence the transcription is kept as a note but not
# promoted to lyrics even for a track with none. Measured, not guessed: set
# from what this run reports, and printed for every track so the number can be
# argued with rather than trusted.
PROMOTE_MIN_CONFIDENCE = 0.35

# A whisper result may replace an earlier whisper result; any other source is
# never overwritten. Without the first half of that rule the first run always
# wins -- including a worse one -- and re-running after improving the word
# filter or changing the model would silently do nothing, which is exactly what
# happened here: a killed run had already written two tracks, and the retry
# reported "already has lyrics" and skipped them.
#
# Re-running is a deliberate act, so the newer transcription is taken as the
# intended one rather than compared on confidence -- which is not comparable
# across models anyway, and is not stored alongside the lyrics.


def promotion_decision(existing_source: str, has_text: bool,
                       confidence: Optional[float]) -> tuple[bool, str]:
    """Whether a transcription should serve as a track's lyrics, and why.

    Returns ``(promote, reason)``. Kept separate from the database work so the
    rule can be tested and argued with directly -- it got this wrong once
    already by treating whisper's own earlier output as a source to protect.
    """
    if has_text and existing_source and existing_source != "whisper":
        return False, f"has lyrics from {existing_source}"
    if confidence is not None and confidence < PROMOTE_MIN_CONFIDENCE:
        return False, (f"confidence {confidence:.2f} below "
                       f"{PROMOTE_MIN_CONFIDENCE}")
    if has_text and existing_source == "whisper":
        return True, "replaces an earlier whisper transcription"
    return True, "no other source"


def audible_spans(recording_id: int, segment: rs.Segment,
                  files: list, stored: dict) -> tuple[float, float]:
    """Narrow a track's wall-clock span to the part that has audio in it.

    A track whose capture was cut short -- skipped, or stopped by the player --
    is otherwise handed to Whisper with minutes of silence attached.
    """
    start, end = segment.start_wall, segment.end_wall
    for seg_file in files:
        gaps = stored.get(seg_file.path.name, [])
        for gap in gaps:
            gap_start = seg_file.start_wall + gap.start
            gap_end = seg_file.start_wall + gap.end
            # A gap that runs to the end of the track's span pulls the end in.
            if gap_start <= end and gap_end >= end - 1.0 and gap_start > start:
                end = gap_start
            # A gap covering the beginning pushes the start out.
            if gap_end >= start and gap_start <= start + 1.0 and gap_end < end:
                start = gap_end
    return start, max(start, end)


def transcribe_segment(audio: Path, model_size: str) -> tuple[list, str, float | None]:
    """Words, cleaned text, and mean confidence for one cut track."""
    words = whisper_sync.transcribe_to_words(audio, model_size=model_size)
    if not words:
        return [], "", None
    lines = whisper_sync.group_words_to_lines(words)
    text = whisper_clean.clean_text("\n".join(t for _s, t in lines))
    probs = [w.probability for w in words if w.probability is not None]
    return words, text, (statistics.fmean(probs) if probs else None)


def promote_from_notes(recording_id: int, *, dry_run: bool = False) -> int:
    """Promote already-stored transcriptions without transcribing again.

    Because the transcription is kept as a note with its confidence, the
    decision about whether it should serve as a track's lyrics can be revisited
    from the database alone. That matters when the promotion rule changes --
    re-deciding then costs a query rather than ten minutes of CPU per album.
    """
    conn = localcache.connect()
    marks = [m for m in recorder.load_marks(recording_id, conn) if m.ok]
    promoted = 0
    for seg in rs.segments(marks):
        track_id = localcache.find_track_id(seg.artist, seg.title, conn)
        if track_id is None:
            continue
        note = next((n for n in localcache.notes_for_track(track_id, conn)
                     if n["kind"] == "transcription" and n["source"] == "whisper"),
                    None)
        if note is None:
            continue

        existing = localcache.get_lyrics_by_track_id(track_id, conn)
        has_text = bool(existing and (existing.plain or existing.synced_raw))
        confidence = note["confidence"]
        label = f"{seg.artist} - {seg.title}"
        promote, reason = promotion_decision(
            (existing.source or "") if existing else "", has_text, confidence)

        if not promote:
            print(f"{label}: note only — {reason}")
        elif dry_run:
            print(f"{label}: would promote ({len(note['text'])} chars, "
                  f"confidence {confidence}) — {reason}")
        else:
            localcache.put_cached_lyrics(
                seg.artist, seg.title,
                Lyrics(plain=note["text"], source="whisper"), conn=conn)
            promoted += 1
            print(f"{label}: promoted ({len(note['text'])} chars, "
                  f"confidence {confidence}) — {reason}")
    print(f"\ndone: {promoted} promoted")
    conn.close()
    return 0


def process(recording_id: int, *, model_size: str = "small",
            dry_run: bool = False) -> int:
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
    if not stored:
        print("no silence map stored; scanning first")
        silence.scan(recording_id, conn=conn, skip_last=False)
        stored = silence.stored_map(recording_id, conn)

    marks = [m for m in recorder.load_marks(recording_id, conn) if m.ok]
    segments = rs.segments(marks)
    print(f"recording {recording_id}: {len(segments)} track(s), "
          f"model={model_size}\n")

    promoted = failed = 0
    for seg in segments:
        start, end = audible_spans(recording_id, seg, files, stored)
        length = end - start
        label = f"{seg.artist} - {seg.title}"
        trimmed = (seg.end_wall - seg.start_wall) - length
        print(f"{label}")
        print(f"  span {length / 60:.1f} min"
              + (f"  ({trimmed:.0f}s of silence trimmed)" if trimmed > 1 else ""))

        if length < 20:
            print("  too short to transcribe; skipped\n")
            continue

        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "cut.flac"
            if not recording_worker.cut(files, start, end, audio):
                print("  could not cut audio; skipped\n")
                failed += 1
                continue

            t0 = time.time()
            try:
                words, text, confidence = transcribe_segment(audio, model_size)
            except Exception as exc:            # a long run should not die on one track
                print(f"  transcription failed: {exc}\n")
                failed += 1
                continue

        if not text.strip():
            print(f"  nothing transcribed in {time.time() - t0:.0f}s\n")
            continue

        conf_text = f"{confidence:.2f}" if confidence is not None else "n/a"
        print(f"  {len(words)} words, {len(text)} chars, "
              f"confidence {conf_text}, {time.time() - t0:.0f}s")
        first = text.strip().splitlines()[0][:60]
        print(f"  first line: {first!r}")

        if dry_run:
            print("  (dry run — nothing written)\n")
            continue

        track_id = localcache.find_track_id(seg.artist, seg.title, conn)
        if track_id is None:
            print("  no track row; note not stored\n")
            continue

        localcache.record_note(track_id, "transcription", text, "whisper",
                               conn, confidence=confidence)
        print("  stored as a transcription note")

        existing = localcache.get_lyrics_by_track_id(track_id, conn)
        has_text = bool(existing and (existing.plain or existing.synced_raw))
        promote, reason = promotion_decision(
            (existing.source or "") if existing else "", has_text, confidence)
        if promote:
            localcache.put_cached_lyrics(
                seg.artist, seg.title,
                Lyrics(plain=text, source="whisper"),
                conn=conn)
            promoted += 1
            print(f"  promoted to lyrics — {reason}\n")
        else:
            print(f"  kept as a note only — {reason}\n")

    print(f"done: {promoted} promoted, {failed} failed")
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("recording_id", type=int)
    ap.add_argument("--model", default="small")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--promote-only", action="store_true",
                    help="re-decide promotion from stored notes, no transcribing")
    args = ap.parse_args(argv)
    if args.promote_only:
        log.info("re-promoting recording %s from notes", args.recording_id)
        return promote_from_notes(args.recording_id, dry_run=args.dry_run)
    log.info("processing recording %s", args.recording_id)
    return process(args.recording_id, model_size=args.model,
                   dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
