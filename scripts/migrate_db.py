"""One-time migration script from the old lyrics_cache to the new schema.

This script reads from the existing `lyrics_cache` table and populates the new
`tracks`, `sources`, and `lyrics` tables. It is designed to be idempotent;
it can be run multiple times without creating duplicate data.
"""
import sqlite3
import time

from karaoke import localcache

_NEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    track_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    artist      TEXT NOT NULL,
    title       TEXT NOT NULL,
    album       TEXT,
    duration    REAL,
    UNIQUE(artist, title)
);

CREATE TABLE IF NOT EXISTS sources (
    source_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id    INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    url         TEXT UNIQUE,
    player_name TEXT,
    FOREIGN KEY(track_id) REFERENCES tracks(track_id)
);

CREATE TABLE IF NOT EXISTS lyrics (
    lyric_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id        INTEGER NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'approved', -- approved | staged | rejected
    source          TEXT, -- lrclib | youtube_caption | whisper | user_submitted
    synced_lyrics   TEXT,
    plain_lyrics    TEXT,
    FOREIGN KEY(track_id) REFERENCES tracks(track_id)
);
"""

def migrate():
    """Migrate the old lyrics_cache to the new schema."""
    conn = localcache.connect()
    try:
        # Check if migration is needed
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tracks'")
        if cur.fetchone():
            print("New schema already exists. Migration not needed.")
            return

        print("Creating new tables...")
        conn.executescript(_NEW_SCHEMA)

        print("Migrating data from lyrics_cache...")
        cur.execute("SELECT * FROM lyrics_cache")
        rows = cur.fetchall()

        for row in rows:
            # Insert into tracks
            cur.execute(
                "INSERT OR IGNORE INTO tracks (artist, title, album, duration) VALUES (?, ?, ?, ?)",
                (row["artist"], row["title"], row["album"], row["duration"])
            )
            cur.execute(
                "SELECT track_id FROM tracks WHERE artist = ? AND title = ?",
                (row["artist"], row["title"])
            )
            track_id = cur.fetchone()["track_id"]

            # Insert into lyrics
            cur.execute(
                """
                INSERT INTO lyrics (track_id, kind, source, synced_lyrics, plain_lyrics)
                VALUES (?, 'approved', ?, ?, ?)
                """,
                (track_id, row["lyrics_source"], row["synced_lyrics"], row["plain_lyrics"])
            )

        print(f"Migrated {len(rows)} tracks.")

        # Rename old table
        conn.execute("ALTER TABLE lyrics_cache RENAME TO _lyrics_cache_old")
        conn.commit()
        print("Migration complete.")

    finally:
        conn.close()

if __name__ == "__main__":
    migrate()
