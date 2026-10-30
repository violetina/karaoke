#!/usr/bin/env python3
"""Validate the waveform aligner (av_align.py) against real audio/video pairs.

PLAN-av-sync.md step 3.2: the synthetic test (tests/test_av_align.py) pinned
the sign convention and a rough scale on a signal with known ground truth.
This is the next rung: real downloads, real codecs, real mastering
differences, compared against the caption-based offset where one already
exists (the system av_align is meant to replace/augment -- caption_sync.
align_audio_and_video_lyrics, same "video_time - audio_time" convention).

For each (artist, title):
  1. Resolve the Audio (Song/ATV) and Video (Music Video/OMV) ids via
     YTMusicClient.get_audio_and_video_versions.
  2. Download both through the existing cache (run_download_logic -- reuses
     a cached file if postprocess already fetched one for this track).
  3. Decode to mono 22.05 kHz and run av_align.estimate_offset.
  4. Compare against the caption-based offset: whatever is already stored in
     track_sync_offsets (mode="video"), or freshly computed by running
     sync_audio_video_counterparts if nothing is stored yet (this is the
     same backfill operation scripts/batch_sync_audio_video.py already runs
     nightly in production -- it only adds video lyrics/an offset row, it
     does not touch playback).

Cases where the two disagree are the interesting ones (per the plan) -- this
script reports both numbers and the delta, it does not auto-resolve which is
right.

Usage:
    python -m scripts.eval_av_align_real_pairs
    python -m scripts.eval_av_align_real_pairs --track-id 923 --track-id 1816
    python -m scripts.eval_av_align_real_pairs --artist-title "Balthazar" "Bunker"
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, Optional

from karaoke import av_align, localcache
from karaoke.postprocess_worker import run_download_logic
from karaoke.ytmusic_client import YTMusicClient

DEFAULT_CANDIDATES: list[tuple[str, str]] = [
    ("PJ Harvey", "Man-Size"),
    ("Queen", "Don't Stop Me Now"),
    ("Balthazar", "Bunker"),
]


def _resolve_track_id(conn: Any, artist: str, title: str) -> Optional[int]:
    cur = conn.cursor()
    cur.execute(
        "SELECT track_id FROM tracks WHERE artist ILIKE %s AND title ILIKE %s ORDER BY track_id LIMIT 1",
        (f"%{artist}%", f"%{title}%"),
    )
    row = cur.fetchone()
    return row["track_id"] if row else None


def _independent_caption_offset(conn: Any, track_id: int, video_id: Optional[str]) -> tuple[Optional[float], str]:
    """Compute a caption-based offset directly, independent of
    sync_audio_video_counterparts -- which now prefers the waveform method
    per PLAN-av-sync.md's order of preference, so calling it here would just
    echo the waveform result back for any track with a distinct video id,
    making the comparison this script exists for trivially zero."""
    from karaoke.caption_sync import fetch_video_caption_lyrics
    from karaoke.lyrics import align_audio_and_video_lyrics

    existing = localcache.get_sync_offset(track_id, conn, mode="video")
    audio_lyrics = localcache.get_lyrics_by_track_id(track_id, conn, media_type="audio")
    if not (audio_lyrics and audio_lyrics.has_synced):
        return existing, "no synced audio lyrics to compare against"
    if not video_id:
        return existing, "no video id to fetch captions from"
    try:
        cap_lyrics = fetch_video_caption_lyrics(f"https://www.youtube.com/watch?v={video_id}")
    except Exception as exc:
        return existing, f"caption fetch error: {exc}"
    if not (cap_lyrics and cap_lyrics.has_synced):
        return existing, "video has no synced captions"
    offset = align_audio_and_video_lyrics(audio_lyrics.synced_raw, cap_lyrics.synced_raw)
    return offset, "freshly computed from captions directly (independent of sync_audio_video_counterparts)"


def evaluate_track(artist: str, title: str, track_id: int, conn: Any, client: YTMusicClient) -> dict[str, Any]:
    report: dict[str, Any] = {"artist": artist, "title": title, "track_id": track_id}

    versions = client.get_audio_and_video_versions(artist, title)
    audio_id, video_id = versions.get("audio_video_id"), versions.get("video_video_id")
    report["audio_id"], report["video_id"] = audio_id, video_id
    if not audio_id or not video_id or audio_id == video_id:
        report["status"] = "no_distinct_counterpart"
        return report

    audio_path = run_download_logic(f"https://www.youtube.com/watch?v={audio_id}", None)
    video_path = run_download_logic(f"https://www.youtube.com/watch?v={video_id}", None)
    if not audio_path or not video_path:
        report["status"] = "download_failed"
        return report

    audio_y = av_align.load_mono(audio_path, av_align.TARGET_SR)
    video_y = av_align.load_mono(video_path, av_align.TARGET_SR)
    sr = av_align.TARGET_SR

    result = av_align.estimate_offset(audio_y, video_y, sr)
    report["waveform"] = result

    caption_offset, caption_source = _independent_caption_offset(conn, track_id, video_id)
    report["caption_offset"] = caption_offset
    report["caption_source"] = caption_source

    if result is not None and result.kind != "low_confidence" and caption_offset is not None:
        report["delta_s"] = result.offset_s - caption_offset

    report["status"] = "ok"
    return report


def print_report(report: dict[str, Any]) -> None:
    label = f"{report['artist']} - {report['title']} (track {report['track_id']})"
    print(f"\n{'='*70}\n{label}\n{'='*70}")
    if report["status"] != "ok":
        print(f"  status: {report['status']}")
        return
    print(f"  audio id: {report['audio_id']}  |  video id: {report['video_id']}")
    r = report["waveform"]
    if r is None:
        print("  waveform: None (inputs too short)")
    else:
        print(f"  waveform: kind={r.kind} offset_s={r.offset_s:+.3f} slope={r.slope:+.6f} "
              f"confidence={r.confidence:.3f}")
    co = report["caption_offset"]
    print(f"  caption offset: {co if co is not None else 'unavailable'} ({report['caption_source']})")
    if "delta_s" in report:
        print(f"  DELTA (waveform - caption): {report['delta_s']:+.3f}s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--track-id", type=int, action="append", default=[],
                        help="Evaluate this track id (repeatable); overrides the default candidate list")
    parser.add_argument("--artist-title", nargs=2, action="append", default=[], metavar=("ARTIST", "TITLE"),
                        help="Evaluate this artist/title pair (repeatable)")
    args = parser.parse_args()

    conn = localcache.connect()
    client = YTMusicClient()

    pairs: list[tuple[str, str, Optional[int]]] = []
    for tid in args.track_id:
        cur = conn.cursor()
        cur.execute("SELECT artist, title FROM tracks WHERE track_id = %s", (tid,))
        row = cur.fetchone()
        if row:
            pairs.append((row["artist"], row["title"], tid))
        else:
            print(f"track id {tid} not found, skipping")
    for artist, title in args.artist_title:
        pairs.append((artist, title, _resolve_track_id(conn, artist, title)))
    if not pairs:
        for artist, title in DEFAULT_CANDIDATES:
            pairs.append((artist, title, _resolve_track_id(conn, artist, title)))

    reports = []
    for artist, title, track_id in pairs:
        if track_id is None:
            print_report({"artist": artist, "title": title, "track_id": None, "status": "not_in_library"})
            continue
        report = evaluate_track(artist, title, track_id, conn, client)
        print_report(report)
        reports.append(report)

    conn.close()

    ok = [r for r in reports if r["status"] == "ok"]
    print(f"\n{'='*70}\nSUMMARY: {len(ok)}/{len(reports)} evaluated\n{'='*70}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
