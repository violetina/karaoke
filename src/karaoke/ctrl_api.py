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

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from .logger import log
from .player_open import open_song_url

CTRL_API_VERSION = "0.3.0"

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


class RecordRequest(BaseModel):
    """Start a capture. Everything is optional; the defaults are the useful ones."""

    source: Optional[str] = None       # PipeWire source; default is the playing sink
    keep_audio: bool = False           # audio is a means to metadata, not a library


class StopRequest(BaseModel):
    recording_id: Optional[int] = None  # omit to stop every session this process owns


class SampleRequest(BaseModel):
    """Analyse what is playing right now by recording a short excerpt."""

    artist: Optional[str] = None
    title: Optional[str] = None
    seconds: Optional[float] = None


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


# -- record mode ----------------------------------------------------------
#
# These live here rather than in karaoke.api because capture needs PipeWire, a
# desktop audio session and a long-lived ffmpeg child -- none of which exist in
# a container. Inspecting recordings is read-only over SQLite and stays there.


@app.post("/api/record/start")
def record_start(req: RecordRequest) -> dict[str, Any]:
    """Begin recording the playing output and marking what is on it."""
    from . import recorder

    try:
        session = recorder.start(req.source or "", keep_audio=req.keep_audio)
    except recorder.RecorderError as exc:
        # Nothing playing, or no ffmpeg: the caller's problem to fix, not a bug.
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        log.exception("Control API failed to start recording")
        raise HTTPException(status_code=500, detail=f"Could not record: {exc}")

    return {
        "status": "recording",
        "recording_id": session.recording_id,
        "source": session.source,
        "dir": str(session.directory),
    }


@app.post("/api/record/stop")
def record_stop(req: StopRequest) -> dict[str, Any]:
    """Stop one capture, or every one this process owns."""
    from . import recorder

    if req.recording_id is None:
        stopped = recorder.active_sessions()
        recorder.stop_all()
    else:
        if not recorder.is_running(req.recording_id):
            raise HTTPException(status_code=404,
                                detail="No such recording running here")
        stopped = [req.recording_id]
        recorder.stop(req.recording_id)
    return {"status": "stopped", "stopped": stopped}


@app.get("/api/record/status")
def record_status() -> dict[str, Any]:
    """Captures running in this process.

    Only this process: a session lives in the one that started it, so a TUI's
    recording is not visible here and vice versa. ``/api/recordings`` on the
    read-only API is the view across all of them.
    """
    from . import recorder

    sessions = []
    for recording_id in recorder.active_sessions():
        ok, total = recorder.mark_count(recording_id)
        directory = recorder.session_directory(recording_id)
        sessions.append({
            "recording_id": recording_id,
            "elapsed_s": recorder.elapsed(recording_id) or 0.0,
            "source": recorder.session_source(recording_id),
            "marks": total,
            "identified": ok,
            "audio_bytes": recorder.directory_size(directory) if directory else 0,
        })
    return {"recording": sessions, "count": len(sessions)}


@app.post("/api/recordings/{recording_id}/analyse")
def record_analyse(recording_id: int, background: BackgroundTasks,
                   keep: bool = False) -> dict[str, Any]:
    """Decompile a recording into the database.

    Returns immediately: analysing a couple of hours takes minutes, which no
    HTTP client should be asked to hold open. Poll
    ``/api/recordings/{id}`` on the read-only API -- the status becomes
    ``analysed`` when it finishes.
    """
    from . import recorder, recording_worker

    record = recording_worker.load_recording(recording_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    if recorder.is_running(recording_id):
        raise HTTPException(status_code=409,
                            detail="Recording is still capturing; stop it first")

    background.add_task(recording_worker.analyse, recording_id,
                        keep=True if keep else None)
    return {"status": "accepted", "recording_id": recording_id}


@app.delete("/api/recordings/{recording_id}/audio")
def record_discard(recording_id: int) -> dict[str, Any]:
    """Delete a recording's audio, keeping its markers."""
    from . import recorder, recording_worker

    if recording_worker.load_recording(recording_id) is None:
        raise HTTPException(status_code=404, detail="Recording not found")
    if recorder.is_running(recording_id):
        raise HTTPException(status_code=409,
                            detail="Recording is still capturing; stop it first")
    freed = recording_worker.discard_audio(recording_id)
    return {"status": "discarded", "recording_id": recording_id,
            "freed_bytes": freed}


@app.post("/api/sample")
def sample_now(req: SampleRequest) -> dict[str, Any]:
    """Detect key/BPM by recording a short excerpt of what is playing.

    Synchronous, unlike analyse: this is bounded by ``seconds`` and the caller
    is waiting on the answer. It is the API form of the TUI's `k`.
    """
    from . import sample_audio

    seconds = req.seconds or sample_audio.DEFAULT_SECONDS
    try:
        result = sample_audio.sample_and_analyse(
            req.artist or "", req.title or "", seconds)
    except sample_audio.CaptureError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except sample_audio.AnalysisUnavailable as exc:
        # The audio was captured fine; this host just cannot examine it.
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        log.exception("Control API sample failed")
        raise HTTPException(status_code=500, detail=f"Sample failed: {exc}")

    return {
        "status": "analysed",
        "artist": req.artist or "",
        "title": req.title or "",
        "seconds": seconds,
        "key": result.key.name if result.key else None,
        "bpm": result.bpm,
        "stored": bool(req.artist and req.title),
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
