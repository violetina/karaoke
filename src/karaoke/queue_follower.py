"""Headless follower for the local karaoke queue.

Plays the active queue row by row through the kiosk browser (CDP) and advances
``active_queue_state.current_index`` when each track ends -- the job the TUI's
Follow Queue loop does, but as a systemd service so the dashboard's Up Next
stays truthful (and playback actually follows the queue) without a TUI session.

Driven by the dashboard through ``karaoke-queue-follow.service``; see the
``/api/follow`` endpoints in :mod:`karaoke.dashboard_api`.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from . import localcache
from .logger import log
from .player_open import (browser_playback, cdp_set_ytmusic_autoplay,
                          open_song_url, track_finished, track_idle)

POLL_INTERVAL_S = 1.0    # snappy pickup of manual Next/Previous (index changes)
LOAD_GRACE_S = 10.0      # let a freshly navigated track load before judging it
IDLE_STALL_S = 12.0      # window holding nothing this long => treat as ended
TAIL_S = 1.5
AUTOPILOT_CHECK_S = 15.0  # how often to check whether to refill (keep going)


def _queue_signature(q: Optional[dict[str, Any]]) -> Optional[tuple]:
    """Identity of the loaded queue, so a replacement restarts following."""
    if not q:
        return None
    return (q.get("playlist_id") or "", q.get("name") or "", len(q.get("rows") or []))


def _play_row(row: dict[str, Any]) -> bool:
    """Open a queue row in the kiosk. Returns False when it has no source."""
    url = str(row.get("url") or "").strip()
    if not url:
        log.warning("queue-follow: no source URL for %s - %s; skipping",
                    row.get("artist"), row.get("title"))
        return False
    try:
        open_song_url(url, row.get("kind"), artist=str(row.get("artist") or ""),
                      title=str(row.get("title") or ""), prefer_audio=True)
        return True
    except Exception:
        log.warning("queue-follow: failed to open %s - %s",
                    row.get("artist"), row.get("title"), exc_info=True)
        return False


def _save_index(q: dict[str, Any], idx: int) -> None:
    localcache.save_active_queue(
        q.get("playlist_id") or "", q.get("name") or "",
        q.get("rows") or [], current_index=idx,
    )


def _record_advance(row: dict[str, Any], idx: int) -> None:
    try:
        localcache.record_queue_event(
            "advance", row.get("track_id"), str(row.get("artist") or ""),
            str(row.get("title") or ""), idx,
        )
    except Exception:
        log.debug("queue-follow: record_queue_event failed", exc_info=True)


def _maybe_autopilot_refill() -> None:
    """Top up the queue when 'keep going' is on and it is near its end.

    Also keeps YouTube Music's own queue-panel Autoplay toggle forced off
    while autopilot is on: YT Music can reset it after a navigation, and if
    it stays on, YT Music may jump to its own mix/radio suggestion at the
    same moment the follower advances to the correct next row, disturbing
    the queue.
    """
    try:
        from . import collection
        with localcache.connect() as conn:
            q = localcache.load_active_queue(conn=conn)
            if q and q.get("autopilot"):
                try:
                    cdp_set_ytmusic_autoplay(False)
                except Exception:
                    log.debug("queue-follow: could not force autoplay off", exc_info=True)
            collection.autopilot_refill(conn)
    except Exception:
        log.debug("queue-follow: autopilot refill check failed", exc_info=True)


def run(poll_interval: float = POLL_INTERVAL_S) -> int:  # pragma: no cover - live loop
    """Follow the active queue until the process is stopped.

    The DB's ``current_index`` is the single source of truth for which row
    should be playing. The follower navigates whenever it changes -- whether an
    ended track advanced it or a manual Next/Previous (dashboard) moved it -- so
    a skip is a single, immediate navigation to the right song instead of a
    double load (YouTube Music autoplay, then a correction).
    """
    log.info("queue-follow: starting")
    sig: Optional[tuple] = None
    launched_idx: Optional[int] = None   # row index we last navigated to
    nav_at = 0.0
    idle_since = 0.0
    last_autopilot = 0.0

    while True:
        # Keep going: refill the queue with fitting tracks as it nears the end.
        if time.monotonic() - last_autopilot > AUTOPILOT_CHECK_S:
            last_autopilot = time.monotonic()
            _maybe_autopilot_refill()

        try:
            q = localcache.load_active_queue()
        except Exception:
            log.warning("queue-follow: could not load active queue", exc_info=True)
            time.sleep(poll_interval)
            continue

        rows = (q or {}).get("rows") or []
        if not q or not rows:
            sig = None
            launched_idx = None
            time.sleep(poll_interval)
            continue

        # A replaced queue (different identity) forces a re-launch even if the
        # stored index happens to match the last one we played.
        new_sig = _queue_signature(q)
        if new_sig != sig:
            sig = new_sig
            launched_idx = None
            idle_since = 0.0
            log.info("queue-follow: following '%s' (%d rows)", q.get("name"), len(rows))

        cur_idx = int(q.get("current_index", 0) or 0)
        if cur_idx >= len(rows):
            # End of queue: wait in case it is extended, do not loop playback.
            time.sleep(poll_interval)
            continue

        row = rows[cur_idx]

        # The target row changed (track ended and advanced it, or a manual
        # Next/Previous moved current_index): navigate to it now.
        if cur_idx != launched_idx:
            if _play_row(row):
                _record_advance(row, cur_idx)
                log.info("queue-follow: playing [%d] %s - %s", cur_idx,
                         row.get("artist"), row.get("title"))
                launched_idx = cur_idx
                nav_at = time.monotonic()
                idle_since = 0.0
            else:
                # No source: skip past it.
                _save_index(q, cur_idx + 1)
                launched_idx = cur_idx  # don't retry this row
            time.sleep(poll_interval)
            continue

        # Monitor the current track.
        try:
            state = browser_playback(timeout=0.3)
        except Exception:
            state = None

        # A freshly navigated track needs time to load; do not judge it yet.
        if time.monotonic() - nav_at < LOAD_GRACE_S:
            time.sleep(poll_interval)
            continue

        # No CDP reply at all (kiosk down / not reachable): wait, never burn
        # through the queue advancing on nothing.
        if state is None:
            time.sleep(poll_interval)
            continue

        if state.get("casting") and not track_finished(state, tail=TAIL_S):
            # The remote cast device owns advancement while it is still playing.
            idle_since = 0.0
            time.sleep(poll_interval)
            continue

        finished = track_finished(state, tail=TAIL_S)
        if not finished:
            if track_idle(state):
                # Window came to rest holding nothing (navigated off / load
                # failed): that never reads as "ended", so stall-detect it.
                now = time.monotonic()
                if not idle_since:
                    idle_since = now
                    time.sleep(poll_interval)
                    continue
                if now - idle_since < IDLE_STALL_S:
                    time.sleep(poll_interval)
                    continue
                log.info("queue-follow: player idle %.0fs; advancing", IDLE_STALL_S)
                finished = True
            else:
                idle_since = 0.0
                time.sleep(poll_interval)
                continue

        # Track finished: advance current_index. Next loop sees the change and
        # navigates to the new row.
        _save_index(q, cur_idx + 1)
        idle_since = 0.0
        if cur_idx + 1 < len(rows):
            log.info("queue-follow: advancing to index %d", cur_idx + 1)
        else:
            log.info("queue-follow: reached end of queue")
        time.sleep(poll_interval)
