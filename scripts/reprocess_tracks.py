#!/usr/bin/env python
"""Audit a set of tracks' audio, then run the full per-track pipeline on it.

Built for the ~1.3k tracks with no CLAP vector, which left them as lone nodes
in the acoustic graph and with no acoustic suggestions. Embedding them is the
easy part; the reason this is more than ``clap_index.py`` is that the library
holds files that should not be analysed as they are:

* **mp3s wrapped in a RIFF/WAV header behind an ID3 tag** -- ``ffmpeg -i``
  refuses them ("invalid start code ID3 in RIFF header"), so every analyser
  fails on them, although forcing the mp3 demuxer decodes them cleanly;
* **truncated files** -- e.g. an 11-second "The Whirl And The Suck";
* **video edits standing in for the audio release** -- intros the album cut
  lacks, so their length disagrees with the track's;
* **lengths that disagree with ``tracks.duration``** -- either the file is
  wrong or the stored duration is, and nothing here can tell which.

Each candidate file is decoded once into a temporary WAV. That pass is the
audit -- it proves the audio decodes, measures its true length and loudness,
and drops any video stream -- and the WAV is then what every step reads, so a
file only has to be decodable once, by one demuxer, for all of them.

Steps, each only when the track lacks it: key/BPM analysis, harmony (key
progression + chords), CLAP, Whisper sync for plain-only lyrics, and the
search document. Word-level caption timings are left to the worker: they are
a rate-limited network call, not audio processing. Harmony is merged into the
progression document rather than replacing it, so a chord detection that
fails now cannot erase chords found on an earlier run.

Only files that pass the audit are processed. ``--include-suspect`` widens
that to files whose only problem is a length mismatch or decode warnings;
broken and silent files are never processed. Every track lands in the report
CSV with what was found and what ran.

Usage::

    python scripts/reprocess_tracks.py --missing-clap --dry-run
    python scripts/reprocess_tracks.py --ids-file tracks.csv --limit 20
    python scripts/reprocess_tracks.py --track-id 173 --track-id 8823
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from karaoke import localcache  # noqa: E402
from karaoke.config import settings  # noqa: E402
from karaoke.logger import log  # noqa: E402
from karaoke.reprocess import (  # noqa: E402
    BACKUP_DISK_ROOT,
    DURATION_TOLERANCE_RATIO,
    DURATION_TOLERANCE_S,
    HOME_MUSIC_ROOT,
    MIN_SECONDS,
    RANK,
    REPORT_FIELDS,
    SILENT_MAX_DB,
    SOURCE_ORDER,
    WAV_CHANNELS,
    WAV_RATE,
    Audit,
    Candidate,
    audit_track,
    cached_youtube_file,
    candidates,
    counterpart_paths,
    decode,
    judge,
    missing_clap_ids,
    probe_streams,
    process,
    read_ids,
    run,
)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--missing-clap", action="store_true",
                     help="every track with no CLAP vector")
    src.add_argument("--ids-file", type=Path, help="CSV with a track_id column")
    src.add_argument("--track-id", type=int, action="append")
    ap.add_argument("--report", type=Path,
                    default=Path(settings.youtube_dir).parent / "reprocess-report.csv")
    ap.add_argument("--dry-run", action="store_true",
                    help="audit only: decode and judge, write nothing but the report")
    ap.add_argument("--include-suspect", action="store_true",
                    help="also process files with length mismatches or decode warnings")
    ap.add_argument("--download", action="store_true",
                    help="fetch audio from YouTube when no file is on hand")
    ap.add_argument("--skip-sync", action="store_true",
                    help="skip Whisper sync of plain-only lyrics")
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after processing this many tracks")
    args = ap.parse_args(argv)

    if args.missing_clap:
        with localcache.connect() as conn:
            ids = missing_clap_ids(conn)
    elif args.ids_file:
        ids = read_ids(args.ids_file)
    else:
        ids = args.track_id
    log.info("reprocessing %d track(s)", len(ids))
    return run(ids, report=args.report, dry_run=args.dry_run,
               include_suspect=args.include_suspect, download=args.download,
               skip_sync=args.skip_sync, limit=args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
