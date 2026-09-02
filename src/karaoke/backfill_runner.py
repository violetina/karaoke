"""The business logic for the automated lyric backfill system."""
from __future__ import annotations
import time
from . import localcache
from . import youtube
from . import web
from .identify import SongRef
from .lyrics import clean_title, fetch_lrclib
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


def _find_lyrics_text(artist: str, title: str) -> str:
    """Find plain lyrics text for a track: LRCLIB first, then Genius scrape.

    LRCLIB is the primary, reliable source (no scraping); Genius is the
    fallback. Returns "" when nothing usable is found.
    """
    # 1. LRCLIB (also try a cleaned title without "- Remastered" etc.)
    for t in {title, clean_title(title)}:
        ly = fetch_lrclib(artist, t)
        if ly.plain:
            print(f"    LRCLIB hit for '{artist} - {t}'")
            return ly.plain
        if ly.synced_raw:
            # strip timestamps to plain text
            from .lyrics import parse_lrc
            plain = "\n".join(txt for _, txt in parse_lrc(ly.synced_raw))
            if plain:
                print(f"    LRCLIB synced hit for '{artist} - {t}'")
                return plain

    # 2. Genius fallback via web search + container parse
    print("  LRCLIB miss; searching Genius...")
    web_results = web.search(f"{artist} {title} lyrics genius")
    for result in web_results:
        if "genius.com" in result["url"]:
            print(f"    Trying Genius link: {result['url']}")
            text = web.fetch_genius_lyrics(result["url"])
            if text:
                return text
    return ""


def _process_gap(gap_id: int, artist: str, title: str) -> None:
    """Process a single lyric gap: find lyrics, then sync to downloaded audio."""
    # 1. Find lyrics FIRST (cheap) — no point downloading audio without them.
    print("  Searching for lyrics...")
    lyrics_text = _find_lyrics_text(artist, title)
    if not lyrics_text:
        raise RuntimeError("No lyrics found (LRCLIB or Genius)")

    # 2. Find + download audio on YouTube
    print(f"  Searching YouTube for '{artist} - {title}'...")
    yt_results = youtube.search(f"{artist} - {title}", limit=1)
    if not yt_results:
        raise RuntimeError("No YouTube results found")
    yt_url = yt_results[0]['url']
    print(f"    Found: {yt_url}")

    print("  Downloading audio...")
    audio_path = youtube.download(yt_url)
    print(f"    Downloaded to: {audio_path}")

    # 3. Generate synced lyrics by aligning the plain text to the audio.
    print("  Generating synced lyrics...")
    import os
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix=".txt") as f:
        f.write(lyrics_text)
        lyrics_file_path = f.name

    try:
        ref = SongRef(
            artist=artist,
            title=title,
            path=str(audio_path),
            source="backfill",
            url=yt_url,
        )
        get_synced(
            ref,
            force_transcribe=True,
            lyrics_file=lyrics_file_path,
        )
    finally:
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
