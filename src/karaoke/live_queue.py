"""The one live karaoke queue.

Every surface that queues a song -- the dashboard's Up Next panel, the
dashboard graph's right-click "Queue next", the AI DJ (booth chip and MCP
tools), the :8888 graph collector and the TUI -- reads and writes the same
single row, ``active_queue_state``. The queue follower
(``karaoke-queue-follow.service``) plays it.

All edits go through :func:`karaoke.localcache.mutate_active_queue`, a locked
read-modify-write, so two writers (say the AI DJ appending while the follower
advances) can no longer overwrite each other's change.

Index conventions: ``rows`` is the whole queue including history;
``current_index`` is the row playing (or about to play). "Upcoming" means
rows after it. Positions in return values are 1-based absolute row numbers.
"""
from __future__ import annotations

import random
from typing import Any, Iterable, Optional

from . import localcache
from .logger import log

#: Identity used for queues built ad hoc (dashboard, AI DJ, graph). Shared with
#: :data:`karaoke.collection.COLLECTION_QUEUE_ID`.
LIVE_QUEUE_ID = "graph-collection"
DEFAULT_NAME = "Karaoke: Dashboard Queue"


def _norm(s: Any) -> str:
    return str(s or "").strip().casefold()


def upcoming_start(rows: list[dict[str, Any]], current_index: int,
                   track_id: Optional[int] = None, artist: str = "",
                   title: str = "") -> int:
    """First row index that is still to come, anchored on what is really playing.

    ``current_index`` only advances while the queue is followed, so after a
    manual or YouTube Music play it goes stale. When the playing song is found
    in the queue (by track_id, then artist+title) the rows after it are
    upcoming; when it is not (an off-queue play), ``current_index`` has not
    been consumed yet and is itself upcoming.
    """
    anchor = None
    if track_id is not None:
        for i, r in enumerate(rows):
            if r.get("track_id") == track_id:
                anchor = i
                break
    if anchor is None and (artist or title):
        for i, r in enumerate(rows):
            if _norm(r.get("artist")) == _norm(artist) and _norm(r.get("title")) == _norm(title):
                anchor = i
                break
    if anchor is not None:
        return anchor + 1
    return max(0, min(int(current_index or 0), len(rows)))


def snapshot(conn=None) -> dict[str, Any]:
    """The whole live queue with absolute indices, for UIs and the AI DJ."""
    q = localcache.load_active_queue(conn=conn) or {}
    rows = q.get("rows") or []
    cur = int(q.get("current_index", 0) or 0)
    return {
        "ok": True,
        "playlist_id": q.get("playlist_id") or "",
        "name": q.get("name") or "",
        "current_index": cur,
        "autopilot": bool(q.get("autopilot")),
        "updated_at": float(q.get("updated_at") or 0.0),
        "track_count": len(rows),
        "upcoming_count": max(0, len(rows) - cur - 1),
        "rows": [{"index": i, **r} for i, r in enumerate(rows)],
    }


def _record(event: str, rows: Iterable[tuple[int, dict[str, Any]]], source: str, conn) -> None:
    for idx, row in rows:
        try:
            localcache.record_queue_event(
                event, track_id=row.get("track_id"), artist=row.get("artist") or "",
                title=row.get("title") or "", queue_index=idx,
                metadata={"source": source}, conn=conn,
            )
        except Exception:
            log.debug("live_queue: record_queue_event failed", exc_info=True)


def enqueue(track_ids: list[int], conn, *, position: str = "end",
            source: str = "dashboard", queue_name: str = DEFAULT_NAME) -> dict[str, Any]:
    """Add library tracks to the live queue, after the current song or at the end.

    - ``position="next"`` inserts right after the current row, in the given
      order. A track that is already upcoming further down is MOVED up instead
      of duplicated ("play this next").
    - ``position="end"`` appends; tracks already upcoming are left where they
      are and reported as duplicates.
    - The currently playing row is never re-added.
    """
    from .collection import rows_for

    clean_ids = [int(t) for t in track_ids if t is not None]
    built = rows_for(clean_ids, conn)
    if not built:
        return {"ok": False, "status": "error", "message": "No matching tracks found in library"}
    pos = "next" if (position or "").strip().lower() == "next" else "end"

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        rows = state["rows"]
        created = not rows
        if created:
            state["playlist_id"] = LIVE_QUEUE_ID
            state["name"] = queue_name
            state["current_index"] = 0
        cur = int(state["current_index"] or 0)
        insert_at = min(len(rows), cur + 1) if (pos == "next" and rows) else len(rows)
        added: list[tuple[int, dict]] = []
        moved: list[tuple[int, dict]] = []
        dupes: list[dict] = []
        for row in built:
            tid = row["track_id"]
            existing = next((i for i in range(min(cur, len(rows)), len(rows))
                             if rows[i].get("track_id") == tid), None)
            if existing is not None:
                if pos == "next" and existing > cur and existing >= insert_at:
                    if existing != insert_at:
                        rows.insert(insert_at, rows.pop(existing))
                        moved.append((insert_at, rows[insert_at]))
                    else:
                        dupes.append(row)
                    insert_at += 1
                else:
                    dupes.append(row)
                continue
            rows.insert(insert_at, row)
            added.append((insert_at, row))
            insert_at += 1
        return {"created": created, "added": added, "moved": moved, "dupes": dupes,
                "track_count": len(rows), "queue_name": state["name"]}

    res = localcache.mutate_active_queue(apply, conn=conn)
    _record("enqueued", res["added"], source, conn)
    touched = res["added"] + res["moved"]
    return {
        "ok": True,
        "status": "queued",
        "created": res["created"],
        "added_count": len(res["added"]),
        "moved_count": len(res["moved"]),
        "duplicate": not touched,
        "track_count": res["track_count"],
        "queue_name": res["queue_name"],
        "tracks": [
            {"track_id": r["track_id"], "artist": r["artist"], "title": r["title"],
             "position": i + 1}
            for i, r in sorted(touched, key=lambda t: t[0])
        ],
    }


def _resolve_index(rows: list[dict[str, Any]], index: int, track_id: Optional[int]) -> Optional[int]:
    """``index`` when it still holds ``track_id``; else where that track went.

    UIs send the index they rendered plus the track_id, so an edit that lands
    after the queue shifted (the follower advanced, the AI DJ inserted) still
    hits the intended song -- or nothing.
    """
    if 0 <= index < len(rows) and (track_id is None or rows[index].get("track_id") == track_id):
        return index
    if track_id is None:
        return None
    hits = [i for i, r in enumerate(rows) if r.get("track_id") == track_id]
    return min(hits, key=lambda i: abs(i - index)) if hits else None


def remove(index: int, conn, *, track_id: Optional[int] = None,
           source: str = "dashboard") -> dict[str, Any]:
    """Remove one row. Removing a history row keeps the current song current."""
    def apply(state: dict[str, Any]) -> Optional[dict[str, Any]]:
        rows = state["rows"]
        i = _resolve_index(rows, int(index), track_id)
        if i is None:
            return None
        row = rows.pop(i)
        if i < int(state["current_index"] or 0):
            state["current_index"] = int(state["current_index"]) - 1
        return {"index": i, "row": row, "track_count": len(rows)}

    res = localcache.mutate_active_queue(apply, conn=conn)
    if res is None:
        return {"ok": False, "status": "error", "message": "That song is no longer in the queue"}
    _record("removed", [(res["index"], res["row"])], source, conn)
    return {"ok": True, "status": "removed", "position": res["index"] + 1,
            "artist": res["row"]["artist"], "title": res["row"]["title"],
            "track_count": res["track_count"]}


def move(index: int, to_index: int, conn, *, track_id: Optional[int] = None,
         source: str = "dashboard") -> dict[str, Any]:
    """Move an upcoming row to another upcoming slot (never into history).

    ``to_index`` may equal ``current_index`` only for the off-queue case the
    dashboard sees (its ``upcoming_start``): there the row at current_index has
    not been played yet. Use :func:`play_next` for "right after the song that
    is playing".
    """
    def apply(state: dict[str, Any]) -> Optional[dict[str, Any]]:
        rows = state["rows"]
        cur = int(state["current_index"] or 0)
        i = _resolve_index(rows, int(index), track_id)
        if i is None or i < cur:
            return None
        dest = max(cur, min(int(to_index), len(rows) - 1))
        if dest != i:
            rows.insert(dest, rows.pop(i))
        return {"index": dest, "row": rows[dest]}

    res = localcache.mutate_active_queue(apply, conn=conn)
    if res is None:
        return {"ok": False, "status": "error", "message": "That song is no longer upcoming"}
    _record("moved", [(res["index"], res["row"])], source, conn)
    return {"ok": True, "status": "moved", "position": res["index"] + 1,
            "artist": res["row"]["artist"], "title": res["row"]["title"]}


def play_next(index: int, conn, *, track_id: Optional[int] = None,
              source: str = "dashboard") -> dict[str, Any]:
    """Move an upcoming row to right after the current song."""
    q = localcache.load_active_queue(conn=conn) or {}
    cur = int(q.get("current_index", 0) or 0)
    return move(index, cur + 1, conn, track_id=track_id, source=source)


def clear_upcoming(conn, *, from_index: Optional[int] = None,
                   source: str = "dashboard") -> dict[str, Any]:
    """Drop every row after the current song; the playing song stays.

    ``from_index`` is the first upcoming row as the caller sees it (the
    dashboard sends the stage's ``upcoming_start``, which is ``current_index``
    itself when the playing song is off-queue). It is never allowed to reach
    back before ``current_index``.
    """
    def apply(state: dict[str, Any]) -> dict[str, Any]:
        rows = state["rows"]
        cur = int(state["current_index"] or 0)
        start = cur + 1 if from_index is None else max(int(from_index), min(cur, len(rows)))
        start = min(start, len(rows))
        removed = len(rows) - start
        del rows[start:]
        if state["current_index"] > len(rows):
            state["current_index"] = len(rows)
        return {"removed": removed, "track_count": len(rows)}

    res = localcache.mutate_active_queue(apply, conn=conn)
    if res["removed"]:
        try:
            localcache.record_queue_event("cleared", metadata={"source": source,
                                                               "removed": res["removed"]}, conn=conn)
        except Exception:
            log.debug("live_queue: record_queue_event failed", exc_info=True)
    return {"ok": True, "status": "cleared", **res}


def shuffle_upcoming(conn, *, source: str = "dashboard",
                     rng: Optional[random.Random] = None) -> dict[str, Any]:
    """Shuffle the rows after the current song."""
    rnd = rng or random.SystemRandom()

    def apply(state: dict[str, Any]) -> int:
        rows = state["rows"]
        cur = int(state["current_index"] or 0)
        tail = rows[cur + 1:]
        rnd.shuffle(tail)
        rows[cur + 1:] = tail
        return len(tail)

    n = localcache.mutate_active_queue(apply, conn=conn)
    return {"ok": True, "status": "shuffled", "shuffled": n}


def append_rows(new_rows: list[dict[str, Any]], conn, *, source: str = "autopilot") -> int:
    """Append prepared rows (e.g. autopilot picks), skipping ones already queued."""
    def apply(state: dict[str, Any]) -> int:
        rows = state["rows"]
        if not rows:
            return 0
        existing = {r.get("track_id") for r in rows if r.get("track_id") is not None}
        fresh = [r for r in new_rows if r.get("track_id") not in existing]
        rows.extend(fresh)
        return len(fresh)

    return localcache.mutate_active_queue(apply, conn=conn)


def set_index(index: int, conn=None) -> None:
    """Move only the playing position (follower / Next / Previous)."""
    localcache.set_active_queue_index(index, conn=conn)
