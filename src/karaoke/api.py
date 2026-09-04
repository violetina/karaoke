"""FastAPI library backend for the karaoke song library.

This service is deployment-safe: it only reads the SQLite library database and
the application log file. It deliberately contains **no** desktop-bound
behaviour (``xdg-open``/``playerctl``), so it can run inside a container where
no display or media player exists.

Playback control lives in :mod:`karaoke.ctrl_api`, which runs on the host
alongside the user's desktop session.
"""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from . import localcache
from .logger import LOG_FILE
from .staging_api import router as staging_router

API_VERSION = "0.3.0"

app = FastAPI(
    title="Karaoke Library API",
    description=(
        "Read-only REST API over the karaoke SQLite library: tracks, lyrics, "
        "stats and logs. Playback is handled by the host-side control API."
    ),
    version=API_VERSION,
)

app.include_router(staging_router)


class TrackResponse(BaseModel):
    track_id: int
    artist: str
    title: str
    album: Optional[str] = None
    duration: Optional[float] = None
    url: Optional[str] = None
    kind: Optional[str] = None
    has_synced_lyrics: bool = False


@app.get("/health")
@app.get("/api/health")
def health() -> dict[str, Any]:
    """Liveness/readiness probe.

    Reports whether the configured SQLite database is reachable so that a
    misconfigured volume mount surfaces as an unhealthy pod rather than as
    empty track listings.
    """
    db_ok = True
    detail = None
    try:
        with localcache.connect() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:  # pragma: no cover - defensive
        db_ok = False
        detail = str(exc)

    return {
        "status": "ok" if db_ok else "degraded",
        "version": API_VERSION,
        "database": "ok" if db_ok else "unavailable",
        "detail": detail,
    }


@app.get("/api/tracks", response_model=list[TrackResponse])
def list_tracks(
    q: Optional[str] = Query(None, description="Search query for artist or title"),
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    """List tracks in the local library with optional search filtering."""
    with localcache.connect() as conn:
        cur = conn.cursor()
        base = """
            SELECT t.track_id, t.artist, t.title, t.album, t.duration, s.url, s.kind,
                   EXISTS(
                       SELECT 1 FROM lyrics l
                       WHERE l.track_id = t.track_id AND l.synced_lyrics != ''
                   ) AS has_synced
            FROM tracks t
            LEFT JOIN sources s ON t.track_id = s.track_id
        """
        if q:
            pattern = f"%{q.strip()}%"
            cur.execute(
                base
                + """
                WHERE t.artist LIKE ? OR t.title LIKE ?
                GROUP BY t.track_id
                ORDER BY t.artist, t.title
                LIMIT ? OFFSET ?
                """,
                (pattern, pattern, limit, offset),
            )
        else:
            cur.execute(
                base
                + """
                GROUP BY t.track_id
                ORDER BY t.artist, t.title
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
        return [
            {
                "track_id": row["track_id"],
                "artist": row["artist"],
                "title": row["title"],
                "album": row["album"],
                "duration": row["duration"],
                "url": row["url"],
                "kind": row["kind"],
                "has_synced_lyrics": bool(row["has_synced"]),
            }
            for row in cur.fetchall()
        ]


@app.get("/api/tracks/{track_id}")
def get_track(track_id: int) -> dict[str, Any]:
    """Get detailed information for a track including lyrics and sources."""
    with localcache.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT track_id, artist, title, album, duration FROM tracks WHERE track_id = ?",
            (track_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Track not found")

        cur.execute(
            "SELECT url, kind, player_name FROM sources WHERE track_id = ?",
            (track_id,),
        )
        sources = [dict(s) for s in cur.fetchall()]

        lyrics = localcache.get_lyrics_by_track_id(track_id, conn)
        lyrics_data = None
        if lyrics:
            lyrics_data = {
                "plain": lyrics.plain,
                "synced_raw": lyrics.synced_raw,
                "source": lyrics.source,
                "has_synced": lyrics.has_synced,
                "line_count": len(lyrics.lines),
            }

        return {
            "track_id": row["track_id"],
            "artist": row["artist"],
            "title": row["title"],
            "album": row["album"],
            "duration": row["duration"],
            "sources": sources,
            "lyrics": lyrics_data,
        }


@app.get("/api/stats")
def get_stats() -> dict[str, Any]:
    """Return local cache summary statistics."""
    summary = localcache.summarize()
    return {
        "total_events": summary.total_events,
        "plays": summary.plays,
        "discoveries": summary.discoveries,
        "cache_hits": summary.cache_hits,
        "cache_misses": summary.cache_misses,
        "cache_hit_rate": summary.cache_hit_rate,
        "distinct_tracks": summary.distinct_tracks,
        "distinct_artists": summary.distinct_artists,
        "top_tracks": summary.top_tracks,
        "top_artists": summary.top_artists,
        "by_mode": summary.by_mode,
    }


@app.get("/api/workers")
def get_workers() -> dict[str, Any]:
    """Post-processing worker and queue statistics.

    Best-effort by design: if RabbitMQ is unreachable, or the workers run on a
    different host (inside a container ``/proc`` shows none of them), this
    reports ``available: false`` with a reason rather than failing. The rest of
    the API stays read-only over SQLite, so a broker outage cannot take the
    library endpoints down with it.

    CPU and memory are summed across every worker, since they scale
    horizontally and a single worker's usage would understate the load.
    """
    from .postprocess_status import QUEUE_NAME, get_status

    st = get_status(sample_cpu=True)
    return {
        "available": st.available,
        "reason": st.reason,
        "queue": {
            "name": QUEUE_NAME,
            "ready": st.ready,
            "unacked": st.unacked,
            "queued": st.queued,
            "consumers": st.consumers,
            "deliver_rate": round(st.deliver_rate, 3),
            "publish_rate": round(st.publish_rate, 3),
            "busy": st.busy,
        },
        "workers": {
            "count": st.workers,
            "running": st.worker_running,
            "pids": list(st.worker_pids),
            "cpu_percent": (round(st.worker_cpu, 1)
                            if st.worker_cpu is not None else None),
            "rss_mb": (round(st.worker_rss_mb, 1)
                       if st.worker_rss_mb is not None else None),
        },
    }


@app.get("/api/logs")
def get_logs(lines: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    """Get the most recent log lines from the application log file."""
    if not LOG_FILE.exists():
        return {"file": str(LOG_FILE), "lines": []}
    try:
        content = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"file": str(LOG_FILE), "lines": content[-lines:]}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read logs: {exc}")


def main() -> None:
    """Entrypoint for starting the library API server.

    Binds 0.0.0.0 by default so the process is reachable inside a container;
    override with ``KARAOKE_API_HOST``/``KARAOKE_API_PORT``.
    """
    import uvicorn

    uvicorn.run(
        "karaoke.api:app",
        host=os.environ.get("KARAOKE_API_HOST", "0.0.0.0"),
        port=int(os.environ.get("KARAOKE_API_PORT", "8000")),
        reload=bool(os.environ.get("KARAOKE_API_RELOAD")),
    )


if __name__ == "__main__":
    main()
