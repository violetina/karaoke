from __future__ import annotations
import os
import time
import asyncio
import threading
import subprocess
from typing import Any, Optional, Dict
from dataclasses import dataclass, field

from .logger import log
from . import localcache
from . import staging
from .lyrics import Lyrics
from .whisper_sync import transcribe_to_lrc
from .stage_sources import stage_youtube_captions
from .youtube import resolve_youtube
from .playerctl import current_songref, SongRef

@dataclass
class JobStatus:
    id: str
    type: str  # whisper, youtube, index, auto-follow
    status: str  # pending, running, completed, failed
    progress: float = 0.0
    message: str = ""
    result: Any = None
    created_at: float = field(default_factory=time.time)

class Supervisor:
    """Background job supervisor for staging and indexing tasks."""

    def __init__(self):
        self.jobs: Dict[str, JobStatus] = {}
        self._lock = threading.Lock()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        
        # Player watching state
        self._watch_active = False
        self._last_ref: Optional[SongRef] = None

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _update_job(self, job_id: str, **kwargs):
        with self._lock:
            if job_id in self.jobs:
                for k, v in kwargs.items():
                    setattr(self.jobs[job_id], k, v)

    def get_job(self, job_id: str) -> Optional[JobStatus]:
        with self._lock:
            return self.jobs.get(job_id)

    def list_jobs(self) -> list[JobStatus]:
        with self._lock:
            # Sort by created_at descending
            return sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)

    def start_whisper_job(self, file_path: str, artist: str, title: str) -> str:
        job_id = f"whisper-{int(time.time()*1000)}"
        status = JobStatus(id=job_id, type="whisper", status="pending", message=f"Queued Whisper: {artist} - {title}")
        with self._lock:
            self.jobs[job_id] = status
        
        asyncio.run_coroutine_threadsafe(self._run_whisper(job_id, file_path, artist, title), self._loop)
        return job_id

    async def _run_whisper(self, job_id: str, file_path: str, artist: str, title: str):
        self._update_job(job_id, status="running", message="Transcribing audio with Whisper...")
        try:
            lrc = await asyncio.to_thread(transcribe_to_lrc, file_path)
            lyrics = Lyrics(plain="", synced_raw=lrc, source="whisper")
            
            self._update_job(job_id, message="Saving to staging...")
            await asyncio.to_thread(self._stage_result, artist, title, lyrics, "whisper", file_path)
            
            self._update_job(job_id, status="completed", progress=1.0, message="Transcription finished and staged.")
        except Exception as e:
            log.exception("Supervisor whisper job %s failed", job_id)
            self._update_job(job_id, status="failed", message=str(e))

    def start_youtube_job(self, url: str) -> str:
        job_id = f"youtube-{int(time.time()*1000)}"
        status = JobStatus(id=job_id, type="youtube", status="pending", message=f"Queued YouTube: {url}")
        with self._lock:
            self.jobs[job_id] = status
            
        asyncio.run_coroutine_threadsafe(self._run_youtube(job_id, url), self._loop)
        return job_id

    async def _run_youtube(self, job_id: str, url: str):
        self._update_job(job_id, status="running", message="Fetching YouTube metadata and captions...")
        try:
            result = await asyncio.to_thread(stage_youtube_captions, url)
            self._update_job(job_id, status="completed", progress=1.0, message=f"Staged {result.artist} - {result.title}")
        except Exception as e:
            log.exception("Supervisor youtube job %s failed", job_id)
            self._update_job(job_id, status="failed", message=str(e))

    def set_watch_player(self, active: bool):
        """Enable or disable the player-following supervisor loop."""
        if active == self._watch_active:
            return
        self._watch_active = active
        if active:
            asyncio.run_coroutine_threadsafe(self._watch_player_loop(), self._loop)
            log.info("Supervisor: Player watching enabled")
        else:
            log.info("Supervisor: Player watching disabled")

    async def _watch_player_loop(self):
        """Continuously check for player changes and missing lyrics."""
        while self._watch_active:
            try:
                ref = await asyncio.to_thread(current_songref)
                if ref and (not self._last_ref or ref.artist != self._last_ref.artist or ref.title != self._last_ref.title):
                    self._last_ref = ref
                    await self._on_track_changed(ref)
            except Exception as e:
                log.debug("Supervisor player watch error: %s", e)
            
            await asyncio.sleep(5)

    async def _on_track_changed(self, ref: SongRef):
        """Check if the new track has lyrics; if not, log it or auto-index."""
        with localcache.connect() as conn:
            # Check if lyrics exist
            lyrics = await asyncio.to_thread(localcache.get_cached_lyrics, ref.artist, ref.title, conn=conn)
            if not lyrics or not lyrics.synced_raw:
                log.info("Supervisor: Track changed to %s - %s (lyrics missing)", ref.artist, ref.title)
                # We could auto-start a YouTube job here if we have a search query,
                # but for now just logging the gap is safer as per "mismatch" risk.
            else:
                log.info("Supervisor: Track changed to %s - %s (lyrics OK)", ref.artist, ref.title)

    def _stage_result(self, artist: str, title: str, lyrics: Lyrics, kind: str, url: str):
        with localcache.connect() as conn:
            staging.stage_lyrics(
                artist=artist,
                title=title,
                lyrics=lyrics,
                source_kind=kind,
                source_url=url,
                conn=conn
            )

# Singleton instance
supervisor = Supervisor()
