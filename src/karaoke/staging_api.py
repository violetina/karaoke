import os
from dataclasses import asdict
from typing import Any, Optional, List
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException

from .supervisor import supervisor
from . import staging

router = APIRouter(prefix="/api/staging")

class StageYoutubeRequest(BaseModel):
    url: str

class StageWhisperRequest(BaseModel):
    file_path: str
    artist: str
    title: str

class JobStatusResponse(BaseModel):
    id: str
    type: str
    status: str
    progress: float
    message: str
    created_at: float

@router.get("/jobs", response_model=List[JobStatusResponse])
def list_jobs():
    """List all background supervisor jobs."""
    return [
        JobStatusResponse(
            id=j.id,
            type=j.type,
            status=j.status,
            progress=j.progress,
            message=j.message,
            created_at=j.created_at
        ) for j in supervisor.list_jobs()
    ]

@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job(job_id: str):
    """Get status of a specific job."""
    job = supervisor.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobStatusResponse(
        id=job.id,
        type=job.type,
        status=job.status,
        progress=job.progress,
        message=job.message,
        created_at=job.created_at
    )

@router.post("/youtube")
def stage_youtube(req: StageYoutubeRequest) -> dict[str, Any]:
    job_id = supervisor.start_youtube_job(req.url)
    return {"status": "accepted", "job_id": job_id, "message": "YouTube staging job dispatched to supervisor."}

@router.post("/whisper")
def stage_whisper(req: StageWhisperRequest) -> dict[str, Any]:
    if not os.path.exists(req.file_path):
        raise HTTPException(status_code=400, detail=f"File not found: {req.file_path}")
    job_id = supervisor.start_whisper_job(req.file_path, req.artist, req.title)
    return {"status": "accepted", "job_id": job_id, "message": "Whisper transcription job dispatched to supervisor."}


@router.get("/queue")
def staging_queue(status: str = "pending", limit: int = 20) -> dict[str, Any]:
    """List staged lyric candidates awaiting review (the admin TUI's staging queue)."""
    items = staging.list_staged(status=status, limit=limit)
    return {"status": "ok", "items": [asdict(i) | {"has_synced": i.has_synced} for i in items]}


@router.post("/queue/{item_id}/approve")
def staging_approve(item_id: int) -> dict[str, Any]:
    """Approve a staged candidate into the local lyrics cache."""
    try:
        item = staging.approve_staged(item_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "ok", "item": asdict(item) | {"has_synced": item.has_synced}}


@router.post("/queue/{item_id}/reject")
def staging_reject(item_id: int) -> dict[str, Any]:
    """Reject a staged candidate without touching the approved lyrics cache."""
    try:
        item = staging.reject_staged(item_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "ok", "item": asdict(item) | {"has_synced": item.has_synced}}
