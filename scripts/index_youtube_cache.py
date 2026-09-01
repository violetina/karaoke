"""Index cached YouTube audio files into the SQLite database."""
import os
from pathlib import Path
from karaoke.config import settings
from karaoke.youtube import resolve_youtube
from karaoke import localcache
from karaoke.lyrics import Lyrics

def main():
    yt_dir = Path(settings.youtube_dir)
    if not yt_dir.exists():
        print(f"YouTube cache dir {yt_dir} does not exist.")
        return

    conn = localcache.connect()
    
    # First, get already indexed URLs to avoid redundant network calls
    cur = conn.cursor()
    cur.execute("SELECT url FROM sources WHERE kind = 'youtube'")
    indexed_urls = {row["url"] for row in cur.fetchall() if row["url"]}
    
    files = list(yt_dir.glob("*.*"))
    print(f"Found {len(files)} files in YouTube cache.")
    
    added = 0
    for i, file_path in enumerate(files):
        # Extract YouTube ID from filename (e.g. "_3tkup9b-iM.webm" -> "_3tkup9b-iM")
        vid_id = file_path.stem
        url = f"https://www.youtube.com/watch?v={vid_id}"
        
        if url in indexed_urls:
            continue
            
        print(f"[{i+1}/{len(files)}] Resolving metadata for {url} ...")
        ref = resolve_youtube(url, download=False)
        if not ref or not ref.artist or not ref.title:
            print(f"  -> Could not resolve artist/title for {vid_id}")
            continue
            
        # Add to localcache with an empty Lyrics object (we just want the track & source)
        try:
            localcache.add_track_and_lyrics(
                artist=ref.artist,
                title=ref.title,
                lyrics=Lyrics(),  # empty lyrics
                duration=ref.duration,
                url=url,
                kind="youtube",
                conn=conn
            )
            print(f"  -> Added: {ref.artist} - {ref.title}")
            added += 1
            indexed_urls.add(url)
        except Exception as e:
            print(f"  -> DB Error: {e}")

    conn.close()
    print(f"Done. Added {added} new tracks from cache.")

if __name__ == "__main__":
    main()
