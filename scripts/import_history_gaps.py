"""One-time script to populate lyric_gaps from play_events history."""
from __future__ import annotations

from karaoke import localcache

def main() -> int:
    """Run the import."""
    with localcache.connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT artist, title
            FROM play_events
            WHERE artist != '' AND title != ''
            """
        )
        history = cur.fetchall()

        imported = 0
        for row in history:
            artist, title = row['artist'], row['title']
            track_id = localcache.find_track_id(artist, title, conn)
            if track_id:
                lyrics = localcache.get_lyrics_by_track_id(track_id, conn)
                if lyrics and (lyrics.synced_raw or lyrics.plain):
                    continue  # Already have lyrics
            
            print(f"Logging gap for: {artist} - {title}")
            localcache.log_lyric_gap(artist, title, conn)
            imported += 1
            
    print(f"\nImported {imported} lyric gaps from play history.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
