"""The business logic for the automated lyric backfill system."""
from __future__ import annotations
import re
import time
from . import localcache
from . import youtube
from . import web
from .player import get_synced

def run() -> None:
    """Run the backfill process."""
    with localcache.connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT gap_id, artist, title FROM lyric_gaps WHERE status = 'pending'")
        gaps = cur.fetchall()

    for gap in gaps:
        print(f"Processing gap: {gap['artist']} - {gap['title']}")
        try:
            _process_gap(gap['gap_id'], gap['artist'], gap['title'])
            _update_gap_status(gap['gap_id'], 'processed')
        except Exception as e:
            print(f"  Failed: {e}")
            _update_gap_status(gap['gap_id'], 'failed')

def _process_gap(gap_id: int, artist: str, title: str) -> None:
    """Process a single lyric gap."""
    # 1. Find on YouTube
    print(f"  Searching YouTube for '{artist} - {title}'...")
    yt_results = youtube.search(f"{artist} - {title}", limit=1)
    if not yt_results:
        raise RuntimeError("No YouTube results found")
    yt_url = yt_results[0]['url']
    print(f"    Found: {yt_url}")

    # 2. Download audio
    print("  Downloading audio...")
    audio_path = youtube.download(yt_url)
    print(f"    Downloaded to: {audio_path}")

    # 3. Find lyrics
    print("  Searching for lyrics...")
    web_results = web.search(f"lyrics for '{artist} - {title}'")
    if not web_results:
        raise RuntimeError("No web results for lyrics found")
    
    lyrics_text = None
    for result in web_results:
        if 'genius.com' in result['url']:
            print(f"    Found Genius link: {result['url']}")
            page = web.extract([result['url']])[0]
            
            # More robust Genius lyric extraction
            match = re.search(r'## .*? Lyrics(.*?)(##|More on Genius)', page['content'], re.DOTALL)
            if match:
                lyrics_text = match.group(1).strip()
            
            break
    
    if not lyrics_text:
        raise RuntimeError("Could not find lyrics on Genius")

    # 4. Generate synced lyrics
    print("  Generating synced lyrics...")
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w+', delete=False) as f:
        f.write(lyrics_text)
        lyrics_file_path = f.name
    
    try:
        get_synced(
            artist, title,
            force_transcribe=True,
            audio_path=str(audio_path),
            lyrics_file=lyrics_file_path
        )
    finally:
        import os
        os.remove(lyrics_file_path)
    
    print("    Done.")


def _update_gap_status(gap_id: int, status: str) -> None:
    """Update the status of a lyric gap."""
    with localcache.connect() as conn:
        conn.execute(
            "UPDATE lyric_gaps SET status = ?, processed_at = ? WHERE gap_id = ?",
            (status, time.time(), gap_id)
        )
        conn.commit()
