"""Host-side control API for karaoke playback.

This service is the counterpart to :mod:`karaoke.api`. It runs **on the user's
desktop machine**, not in the cluster, because launching playback requires a
display server and a media player (``xdg-open`` / ``playerctl``) that do not
exist inside a container.

It binds to loopback by default: it can spawn local processes, so it must not
be exposed to the network.
"""
from __future__ import annotations

import os
from typing import Any, Optional
from urllib.parse import quote_plus

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .logger import log
from .player_open import open_song_url

CTRL_API_VERSION = "0.2.0"

app = FastAPI(
    title="Karaoke Control API",
    description=(
        "Host-side playback control for karaoke. Runs on the desktop session "
        "and launches media players; not intended for cluster deployment."
    ),
    version=CTRL_API_VERSION,
)


class PlayRequest(BaseModel):
    url: Optional[str] = None
    kind: Optional[str] = None
    artist: Optional[str] = None
    title: Optional[str] = None


@app.get("/health")
@app.get("/api/health")
def health() -> dict[str, Any]:
    """Liveness probe reporting whether a desktop session is present."""
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return {
        "status": "ok",
        "version": CTRL_API_VERSION,
        "role": "control",
        "display": "available" if has_display else "headless",
    }


@app.post("/api/play")
def play_track(req: PlayRequest) -> dict[str, Any]:
    """Open/play a song URL, or fall back to a YouTube search for artist/title."""
    url = req.url
    kind = req.kind
    artist = req.artist or ""
    title = req.title or ""

    if not url:
        query = quote_plus(f"{artist} {title}".strip())
        if not query:
            raise HTTPException(
                status_code=400, detail="No URL or artist/title provided"
            )
        url = f"https://www.youtube.com/results?search_query={query}"
        kind = "youtube_search"

    try:
        pid = open_song_url(url, kind)
    except Exception as exc:
        log.exception("Control API play error for %s", url)
        raise HTTPException(status_code=500, detail=f"Failed to launch player: {exc}")

    return {
        "status": "launched",
        "url": url,
        "kind": kind,
        "pid": pid,
        "artist": artist,
        "title": title,
    }


def main() -> None:
    """Entrypoint for the host-side control API.

    Binds loopback only by default, since this endpoint spawns local processes.
    """
    import uvicorn

    uvicorn.run(
        "karaoke.ctrl_api:app",
        host=os.environ.get("KARAOKE_CTRL_HOST", "127.0.0.1"),
        port=int(os.environ.get("KARAOKE_CTRL_PORT", "8765")),
        reload=bool(os.environ.get("KARAOKE_CTRL_RELOAD")),
    )


if __name__ == "__main__":
    main()
