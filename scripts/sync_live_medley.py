"""Sync a live medley recording by aligning each song to its own audio slice.

The 2 Meter Sessions take (recording 27, track #960) is three Morphine songs
back to back: "Have a Lucky Day", "Buena", "Sharks". Aligning one combined
lyric text against the whole 10-minute capture leaves long anchor droughts
(the middle of a song has no matched words, so lines get interpolated and drift
badly). This instead cuts the recording into one window per song, aligns each
song's *own* stored lyrics against only that slice, offsets the timings back
onto the full timeline, and writes the stitched result to the medley track.

Each song's words come from its own track row (three text sources):
  Have a Lucky Day -> track 961
  Buena            -> track 116
  Sharks           -> track 962
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "src")

from karaoke import localcache, recording_worker, track_analysis, vector_index
from karaoke.lyric_align import align_lines
from karaoke.whisper_sync import lines_to_lrc, transcribe_to_words
from karaoke.lyric_language import detect as detect_language

RECORDING_ID = 27
MEDLEY_TRACK = 960

# Song -> (source track_id, [window_start_s, window_end_s]) relative to capture
# start. Windows come from the recording markers (see recording_marks): Lucky
# Day anchors at ~11s, the Buena region at ~300s, Sharks at ~580s, and the set
# hands off to Sonic Youth at ~800s. Windows overlap slightly so a song that
# starts a touch early/late still has its opening inside the slice.
SONGS = [
    ("Have a Lucky Day", 961, (0.0, 305.0)),
    ("Buena", 116, (292.0, 582.0)),
    ("Sharks", 962, (572.0, 812.0)),
]


def _plain_lyrics(conn, track_id: int) -> str:
    row = conn.execute(
        "SELECT plain_lyrics FROM lyrics WHERE track_id = ? AND kind = 'approved'",
        (track_id,),
    ).fetchone()
    return (row["plain_lyrics"] or "").strip() if row else ""


def main() -> int:
    with localcache.connect() as conn:
        record = recording_worker.load_recording(RECORDING_ID)
        if record is None:
            print(f"recording {RECORDING_ID} not found")
            return 1
        files = recording_worker.segment_files(Path(record["dir"]))
        span = recording_worker.recording_span(files)
        if not files or span is None:
            print("no audio segments for recording")
            return 1
        base_wall = span[0]
        print(f"recording {RECORDING_ID}: {len(files)} segment file(s), "
              f"{span[1] - span[0]:.0f}s of audio")

        bpm_row = conn.execute(
            "SELECT a.bpm FROM track_analysis a WHERE a.track_id = ?",
            (MEDLEY_TRACK,)).fetchone()
        track_analysis.ensure_schema(conn)

        stitched: list[tuple[float, str]] = []
        for title, src_track, (w0, w1) in SONGS:
            plain = _plain_lyrics(conn, src_track)
            if not plain:
                print(f"  skip  {title}: no stored lyrics on track {src_track}")
                continue
            lyric_lines = [ln.strip() for ln in plain.splitlines() if ln.strip()]
            start_wall = base_wall + w0
            end_wall = min(base_wall + w1, span[1])
            with tempfile.TemporaryDirectory() as tmp:
                wav = Path(tmp) / "slice.wav"
                if not recording_worker.cut(files, start_wall, end_wall, wav):
                    print(f"  fail  {title}: could not cut window")
                    continue
                language = detect_language(plain)
                words = transcribe_to_words(str(wav), text=plain, language=language)
                report: dict = {}
                lines = align_lines(
                    lyric_lines, words,
                    total_duration=(end_wall - start_wall),
                    bpm=(bpm_row["bpm"] if bpm_row else None),
                    report=report)
            anchored = report.get("anchored", 0)
            # Offset back onto the full-medley timeline.
            offset = w0
            stitched.append((offset, f"[{title}]"))
            for t, text in lines:
                stitched.append((offset + t, text))
            print(f"  ok    {title}: {len(lines)} lines, "
                  f"{anchored} anchored, window {w0:.0f}-{end_wall - base_wall:.0f}s")

        stitched.sort(key=lambda p: p[0])
        lrc = lines_to_lrc(stitched)
        medley = conn.execute(
            "SELECT artist, title FROM tracks WHERE track_id = ?",
            (MEDLEY_TRACK,)).fetchone()
        localcache.add_track_and_lyrics(
            medley["artist"], medley["title"],
            localcache.Lyrics(synced_raw=lrc, plain="\n".join(t for _, t in stitched),
                              source="whisper_aligned"),
            conn=conn)
        print(f"\nstored {len(stitched)} synced lines on track {MEDLEY_TRACK}")

    print("rebuilding vector index...")
    st = vector_index.rebuild_from_sqlite(embed=True, include_lines=True)
    print(f"indexed: tracks={st.indexed}, lines={st.line_docs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
