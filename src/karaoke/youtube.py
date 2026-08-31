"""YouTube search and download."""
from __future__ import annotations
from typing import Optional
from yt_dlp import YoutubeDL
from pathlib import Path
from .config import settings
from .identify import SongRef, parse_query

def search(query: str, limit: int = 1) -> list[dict]:
    """Search YouTube and return a list of results."""
    with YoutubeDL({'quiet': True, 'extract_flat': True}) as ydl:
        results = ydl.extract_info(f"ytsearch{limit}:{query}", download=False).get('entries', [])
    return [{'url': r['url'], 'title': r['title']} for r in results]

def download(url: str) -> str:
    """Download a YouTube video and return the path to the audio file."""
    outtmpl = str(Path(settings.youtube_dir) / '%(id)s.%(ext)s')
    with YoutubeDL({'quiet': True, 'format': 'bestaudio', 'outtmpl': outtmpl}) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)

def resolve_youtube(url: str, download: bool = False) -> Optional[SongRef]:
    """Resolve a YouTube URL to a SongRef, optionally downloading the audio."""
    with YoutubeDL({'quiet': True}) as ydl:
        info = ydl.extract_info(url, download=False)
    
    ref = parse_query(info.get('title', ''))
    if not ref.title:
        return None
        
    ref.url = url
    if download:
        ref.path = download(url)
            
    return ref

def clear_youtube_cache():
    # Placeholder
    pass

def prune_youtube_cache(size_mb: int):
    # Placeholder
    pass

def youtube_cache_summary():
    # Placeholder
    pass
