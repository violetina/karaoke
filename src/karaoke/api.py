"""FastAPI backend service for the karaoke song library and TUI."""
from __future__ import annotations

from typing import Any, Optional
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from . import localcache
from .browse import open_song_url
from .logger import LOG_FILE, log

app = FastAPI(
    title="Karaoke TUI Backend API",
    description="REST API for browsing tracks, lyrics, playing media, and monitoring stats.",
    version="0.1.0",
)


class PlayRequest(BaseModel):
    url: Optional[str] = None
    kind: Optional[str] = None
    artist: Optional[str] = None
    title: Optional[str] = None


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
def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/api/tracks", response_model=list[TrackResponse])
def list_tracks(q: Optional[str] = Query(None, description="Search query for artist or title")) -> list[dict[str, Any]]:
    """List tracks in the local library with optional search filtering."""
    with localcache.connect() as conn:
        cur = conn.cursor()
        if q:
            pattern = f"%{q.strip()}%"
            cur.execute(
                """
                SELECT t.track_id, t.artist, t.title, t.album, t.duration, s.url, s.kind,
                       EXISTS(SELECT 1 FROM lyrics l WHERE l.track_id = t.track_id AND l.synced_lyrics != '') as has_synced
                FROM tracks t
                LEFT JOIN sources s ON t.track_id = s.track_id
                WHERE t.artist LIKE ? OR t.title LIKE ?
                GROUP BY t.track_id
                ORDER BY t.artist, t.title
                """,
                (pattern, pattern),
            )
        else:
            cur.execute(
                """
                SELECT t.track_id, t.artist, t.title, t.album, t.duration, s.url, s.kind,
                       EXISTS(SELECT 1 FROM lyrics l WHERE l.track_id = t.track_id AND l.synced_lyrics != '') as has_synced
                FROM tracks t
                LEFT JOIN sources s ON t.track_id = s.track_id
                GROUP BY t.track_id
                ORDER BY t.artist, t.title
                """
            )
        rows = cur.fetchall()
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
            for row in rows
        ]


@app.get("/api/tracks/{track_id}")
def get_track(track_id: int) -> dict[str, Any]:
    """Get detailed information for a specific track including lyrics and sources."""
    with localcache.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT track_id, artist, title, album, duration FROM tracks WHERE track_id = ?",
            (track_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Track not found")

        cur.execute("SELECT url, kind, player_name FROM sources WHERE track_id = ?", (track_id,))
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


@app.post("/api/play")
def play_track(req: PlayRequest) -> dict[str, Any]:
    """Open/play a song URL or search query using the player opener."""
    url = req.url
    kind = req.kind
    artist = req.artist or ""
    title = req.title or ""

    if not url:
        from urllib.parse import quote_plus
        query = quote_plus(f"{artist} {title}".strip())
        if not query:
            raise HTTPException(status_code=400, detail="No URL or artist/title provided")
        url = f"https://www.youtube.com/results?search_query={query}"
        kind = "youtube_search"

    try:
        pid = open_song_url(url, kind)
        return {
            "status": "launched",
            "url": url,
            "kind": kind,
            "pid": pid,
            "artist": artist,
            "title": title,
        }
    except Exception as e:
        log.exception("API play error for %s", url)
        raise HTTPException(status_code=500, detail=f"Failed to launch player: {e}")


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


@app.get("/api/logs")
def get_logs(lines: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    """Get the most recent log lines from the application log file."""
    if not LOG_FILE.exists():
        return {"file": str(LOG_FILE), "lines": []}
    try:
        content = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"file": str(LOG_FILE), "lines": content[-lines:]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read logs: {e}")


def main() -> None:
    """Entrypoint for starting the API server."""
    import uvicorn
    uvicorn.run("karaoke.api:app", host="127.0.0.1", port=8000, reload=True)


if __name__ == "__main__":
    main()
