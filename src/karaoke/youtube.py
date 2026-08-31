"""YouTube search and download."""
from __future__ import annotations
from yt_dlp import YoutubeDL

def search(query: str, limit: int = 1) -> list[dict]:
    """Search YouTube and return a list of results."""
    with YoutubeDL({'quiet': True}) as ydl:
        results = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)['entries']
    return [{'url': r['webpage_url'], 'title': r['title']} for r in results]

def download(url: str) -> str:
    """Download a YouTube video and return the path to the audio file."""
    with YoutubeDL({'quiet': True, 'format': 'bestaudio', 'outtmpl': '%(id)s.%(ext)s'}) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)
