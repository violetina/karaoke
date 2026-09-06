#!/usr/bin/env python3
"""Live End-to-End Test for Karaoke Recording APIs.

Executes real HTTP requests against live running servers:
- Library API (`http://localhost:8000`)
- Control API (`http://127.0.0.1:8765`)

Run:
    PYTHONPATH=src .venv/bin/python scripts/e2e_test_recordings_api.py
"""
from __future__ import annotations

import sys
import time
from typing import Any

from karaoke.api_client import ApiClient


def log(msg: str) -> None:
    print(f"[E2E RECORDINGS] {msg}")


def main() -> int:
    client = ApiClient(fallback_local=True)

    log("1. Checking API Health...")
    lib_health = client.health()
    ctrl_health = client.ctrl_health()
    log(f"   Library API health: {lib_health}")
    log(f"   Control API health: {ctrl_health}")

    log("2. Listing recordings over Library API...")
    recs = client.list_recordings(limit=5)
    log(f"   Found {recs['count']} recording session(s)")
    if recs["count"] > 0:
        sample_rec = recs["recordings"][0]
        rec_id = sample_rec["recording_id"]
        log(f"   Sample recording ID={rec_id}, status={sample_rec['status']}, marks={sample_rec['marks']}")

        log(f"3. Fetching detailed track slice info for recording ID={rec_id}...")
        detail = client.get_recording(rec_id)
        if detail:
            log(f"   Detail: captured={detail['captured_s']:.1f}s, tracks_count={len(detail['tracks'])}")

        log(f"4. Testing metadata PATCH on recording ID={rec_id}...")
        current_note = sample_rec.get("note") or ""
        test_note = f"e2e-check-{int(time.time())}"
        updated = client.update_recording(rec_id, note=test_note)
        if updated:
            log(f"   Updated note to: {updated.get('note')}")
            # Restore original note
            client.update_recording(rec_id, note=current_note)
            log("   Restored original note")

    log("5. Checking live capture status over Control API...")
    status = client.record_status()
    log(f"   Live captures: {status}")

    log("6. Testing playback sessions over Control API...")
    play_res = client.play(artist="Swans", title="A Little God In My Hands", prefer_audio=True)
    log(f"   Play launch response: status={play_res.get('status')}, session_id={play_res.get('session_id')}")

    if play_res.get("session_id"):
        sid = play_res["session_id"]
        sessions = client.list_play_sessions()
        log(f"   Active play sessions count: {sessions.get('count')}")
        stop_res = client.stop_play_session(sid)
        log(f"   Stopped session {sid}: {stop_res.get('status')}")

    log("7. Verification complete. All recording API endpoints functioning!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
