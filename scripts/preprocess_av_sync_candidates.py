#!/usr/bin/env python3
"""Pre-process the AV Sync Candidates playlist: compute and store each
track's audio/video offset now, via the same sync_audio_video_counterparts
path the postprocess pipeline uses (waveform first, falls through to
captions -- Whisper stays opt-in, off here).

Usage:
    python -m scripts.preprocess_av_sync_candidates
"""
from __future__ import annotations

import sys
import time
from typing import Any

from karaoke import localcache, ytmusic_client
from karaoke.caption_sync import sync_audio_video_counterparts

PLAYLIST_ID = "av-sync-candidates"


def main() -> int:
    conn = localcache.connect()
    client = ytmusic_client.YTMusicClient()

    cur = conn.cursor()
    cur.execute(
        "SELECT track_id, artist, title FROM saved_playlist_tracks "
        "WHERE playlist_id = %s ORDER BY position",
        (PLAYLIST_ID,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    print(f"Pre-processing {len(rows)} candidate(s) from playlist '{PLAYLIST_ID}'...\n")

    for row in rows:
        t0 = time.time()
        label = f"{row['artist']} - {row['title']} (track {row['track_id']})"
        try:
            res = sync_audio_video_counterparts(row["track_id"], conn, client=client, force_waveform=True)
            method = res.get("method") or "none"
            offset = res.get("offset")
            conf = res.get("confidence")
            conf_str = f" conf={conf:.3f}" if conf is not None else ""
            offset_str = f"{offset:+.2f}s" if offset is not None else "none"
            print(f"[{time.time()-t0:5.1f}s] {label:<55} -> method={method:<9} offset={offset_str}{conf_str}",
                 flush=True)
        except Exception as exc:
            print(f"[{time.time()-t0:5.1f}s] {label:<55} -> ERROR: {exc}", flush=True)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
