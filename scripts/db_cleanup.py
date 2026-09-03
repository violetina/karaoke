"""Data cleanup, deduplication, and self-healing script for the karaoke platform database."""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
import time
from typing import Optional

from karaoke.config import settings
from karaoke.youtube import fetch_metadata, parse_youtube_title, search
from karaoke.analyze import analyze_audio
from karaoke import localcache, track_analysis


def are_artists_compatible(art1: str, art2: str) -> bool:
    """Return True if two artist strings are compatible/highly similar."""
    a1 = art1.lower().strip()
    a2 = art2.lower().strip()
    if not a1 or not a2:
        return False
    if a1 == a2:
        return True
    
    # Check if one is a substring of the other (with length guard)
    if len(a1) >= 3 and a1 in a2:
        return True
    if len(a2) >= 3 and a2 in a1:
        return True

    # Strip common suffixes/decorations
    def clean(s: str) -> str:
        s = s.replace("- topic", "").replace("(topic)", "").replace("official", "").strip()
        # Replace non-alphanumeric punctuation with spaces
        s = re.sub(r'[\(\)\[\]\-\&\,\s]+', ' ', s).strip()
        return s

    c1, c2 = clean(a1), clean(a2)
    if not c1 or not c2:
        return False
    if c1 == c2:
        return True

    # Check if first word matches (common for multi-artist lists)
    w1 = c1.split()[0] if c1.split() else ""
    w2 = c2.split()[0] if c2.split() else ""
    if w1 and w2 and w1 == w2 and len(w1) >= 3:
        return True

    return False


def merge_tracks(track_id_src: int, track_id_dest: int, conn: sqlite3.Connection) -> None:
    """Merge Track A (src) into Track B (dest)."""
    cur = conn.cursor()

    # Move sources (deduplicating URLs)
    cur.execute("SELECT url, kind, player_name FROM sources WHERE track_id = ?", (track_id_src,))
    src_sources = cur.fetchall()
    for row in src_sources:
        url, kind, player_name = row["url"], row["kind"], row["player_name"]
        # Check if destination already has this URL
        cur.execute("SELECT source_id FROM sources WHERE track_id = ? AND url = ?", (track_id_dest, url))
        if cur.fetchone():
            # Delete duplicate from source track
            cur.execute("DELETE FROM sources WHERE track_id = ? AND url = ?", (track_id_src, url))
        else:
            # Move source
            cur.execute("UPDATE sources SET track_id = ? WHERE track_id = ? AND url = ?", (track_id_dest, track_id_src, url))

    # Move lyrics (keeping the better ones)
    cur.execute("SELECT lyric_id, kind, source, synced_lyrics, plain_lyrics FROM lyrics WHERE track_id = ?", (track_id_src,))
    src_lyrics = cur.fetchall()
    for row in src_lyrics:
        ly_id, kind, source, synced, plain = row["lyric_id"], row["kind"], row["source"], row["synced_lyrics"], row["plain_lyrics"]
        # Check if destination already has lyrics of this kind
        cur.execute("SELECT lyric_id, synced_lyrics, plain_lyrics FROM lyrics WHERE track_id = ? AND kind = ?", (track_id_dest, kind))
        dest_ly = cur.fetchone()
        if dest_ly:
            # Determine which is better (synced over plain)
            dest_has_synced = bool(dest_ly["synced_lyrics"])
            src_has_synced = bool(synced)
            if src_has_synced and not dest_has_synced:
                # Replace dest lyrics with src lyrics
                cur.execute("UPDATE lyrics SET source = ?, synced_lyrics = ?, plain_lyrics = ? WHERE lyric_id = ?", 
                            (source, synced, plain, dest_ly["lyric_id"]))
            # Delete the source lyrics row
            cur.execute("DELETE FROM lyrics WHERE lyric_id = ?", (ly_id,))
        else:
            # Move lyrics
            cur.execute("UPDATE lyrics SET track_id = ? WHERE lyric_id = ?", (track_id_dest, ly_id))

    # Move track analysis (keeping the more complete one)
    cur.execute("SELECT * FROM track_analysis WHERE track_id = ?", (track_id_src,))
    src_analysis = cur.fetchone()
    if src_analysis:
        cur.execute("SELECT * FROM track_analysis WHERE track_id = ?", (track_id_dest,))
        dest_analysis = cur.fetchone()
        if dest_analysis:
            # Keep whichever has BPM or more fields populated
            dest_has_bpm = bool(dest_analysis["bpm"])
            src_has_bpm = bool(src_analysis["bpm"])
            if src_has_bpm and not dest_has_bpm:
                # Update dest with src values
                cur.execute(
                    """
                    UPDATE track_analysis 
                    SET detected_key=?, key_confidence=?, key_agreement=?, reference_key=?, reference_src=?,
                        resolved_key=?, key_relation=?, bpm=?, method=?, analyzer_version=?, updated_at=?,
                        energy=?, brightness=?
                    WHERE track_id=?
                    """,
                    (src_analysis["detected_key"], src_analysis["key_confidence"], src_analysis["key_agreement"],
                     src_analysis["reference_key"], src_analysis["reference_src"], src_analysis["resolved_key"],
                     src_analysis["key_relation"], src_analysis["bpm"], src_analysis["method"],
                     src_analysis["analyzer_version"], src_analysis["updated_at"], src_analysis["energy"],
                     src_analysis["brightness"], track_id_dest)
                )
            cur.execute("DELETE FROM track_analysis WHERE track_id = ?", (track_id_src,))
        else:
            cur.execute("UPDATE track_analysis SET track_id = ? WHERE track_id = ?", (track_id_dest, track_id_src))

    # Delete the source track row from tracks
    cur.execute("DELETE FROM tracks WHERE track_id = ?", (track_id_src,))


def run_deduplication(conn: sqlite3.Connection) -> None:
    """Scan and merge duplicate tracks representing the same song."""
    print("=== Step 1: Track Deduplication and Merging ===")
    cur = conn.cursor()
    cur.execute("SELECT track_id, artist, title, duration FROM tracks")
    tracks = cur.fetchall()

    by_title = {}
    for t in tracks:
        title_key = t["title"].lower().strip()
        by_title.setdefault(title_key, []).append(t)

    merged_count = 0
    for title, group in by_title.items():
        if len(group) <= 1:
            continue

        # Check pairs in this title group
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                t1, t2 = group[i], group[j]
                
                # Check if we still have both in the database (one might have been deleted/merged already)
                cur.execute("SELECT 1 FROM tracks WHERE track_id = ?", (t1["track_id"],))
                if not cur.fetchone():
                    continue
                cur.execute("SELECT 1 FROM tracks WHERE track_id = ?", (t2["track_id"],))
                if not cur.fetchone():
                    continue

                if are_artists_compatible(t1["artist"], t2["artist"]):
                    # Decide which is source (duplicate) and which is destination (canonical)
                    # We prefer keeping the one with lyrics
                    cur.execute("SELECT 1 FROM lyrics WHERE track_id = ?", (t1["track_id"],))
                    t1_has_lyrics = bool(cur.fetchone())
                    cur.execute("SELECT 1 FROM lyrics WHERE track_id = ?", (t2["track_id"],))
                    t2_has_lyrics = bool(cur.fetchone())

                    if t1_has_lyrics and not t2_has_lyrics:
                        dest, src = t1, t2
                    elif t2_has_lyrics and not t1_has_lyrics:
                        dest, src = t2, t1
                    else:
                        # Prefer longer artist name as canonical spelling (usually has full names)
                        if len(t1["artist"]) >= len(t2["artist"]):
                            dest, src = t1, t2
                        else:
                            dest, src = t2, t1

                    print(f"Merging duplicate track: '{src['artist']}' - '{src['title']}' (ID {src['track_id']})")
                    print(f"                     into: '{dest['artist']}' - '{dest['title']}' (ID {dest['track_id']})")
                    
                    merge_tracks(src["track_id"], dest["track_id"], conn)
                    merged_count += 1

    print(f"Deduplication complete. Merged {merged_count} duplicate track(s).")


def run_source_healing(conn: sqlite3.Connection, limit: int = 50) -> None:
    """Find source URLs for tracks with lyrics that lack any source mapping."""
    print("\n=== Step 2: Auto-filling Source URLs for Orphan Tracks ===")
    cur = conn.cursor()
    cur.execute(
        """
        SELECT t.track_id, t.artist, t.title FROM tracks t
        WHERE EXISTS(SELECT 1 FROM lyrics l WHERE l.track_id = t.track_id)
          AND NOT EXISTS(SELECT 1 FROM sources s WHERE s.track_id = t.track_id)
        """
    )
    rows = cur.fetchall()
    
    # Filter out obvious garbage/mock entries
    eligible = []
    for r in rows:
        artist, title = r["artist"].strip(), r["title"].strip()
        if not title:
            continue
        if title.lower() in ("analyse", "tui", "test", "demo"):
            # Garbage cleanup: delete them!
            print(f"Pruning mock/test track ID {r['track_id']} ('{artist}' - '{title}')")
            cur.execute("DELETE FROM tracks WHERE track_id = ?", (r["track_id"],))
            cur.execute("DELETE FROM lyrics WHERE track_id = ?", (r["track_id"],))
            cur.execute("DELETE FROM track_analysis WHERE track_id = ?", (r["track_id"],))
            continue
        eligible.append(r)

    print(f"Found {len(eligible)} valid tracks with lyrics but no source URL.")
    
    healed = 0
    for i, r in enumerate(eligible[:limit]):
        artist, title = r["artist"], r["title"]
        query = f"{artist} {title}".strip()
        print(f"[{i+1}/{min(len(eligible), limit)}] Searching YouTube for '{query}'...")
        try:
            results = search(query, limit=1)
            if results:
                url = results[0]["url"]
                # Add to sources
                cur.execute(
                    "INSERT INTO sources (track_id, kind, url, player_name) VALUES (?, ?, ?, ?)",
                    (r["track_id"], "youtube", url, "browser")
                )
                print(f"  -> Found URL: {url}")
                healed += 1
                time.sleep(1.0)  # Safe throttling
            else:
                print("  -> No YouTube results found.")
        except Exception as e:
            print(f"  -> Search error: {e}")

    print(f"Source URL auto-fill complete. Healed {healed} track(s).")


def run_orphan_cache_healing(conn: sqlite3.Connection) -> None:
    """Find downloaded cache files that are missing track/source mappings, heal them, and analyze."""
    print("\n=== Step 3: Self-Healing Orphan Downloaded Cache Files ===")
    yt_dir = Path(settings.youtube_dir)
    if not yt_dir.exists():
        print(f"YouTube cache directory {yt_dir} does not exist.")
        return

    cur = conn.cursor()
    # Get all known YouTube source URLs
    cur.execute("SELECT url FROM sources WHERE kind = 'youtube'")
    known_urls = {row["url"] for row in cur.fetchall() if row["url"]}

    files = list(yt_dir.glob("*.webm"))
    print(f"Found {len(files)} files in YouTube cache.")

    healed_count = 0
    for i, file_path in enumerate(files):
        vid_id = file_path.stem
        url = f"https://www.youtube.com/watch?v={vid_id}"

        if url in known_urls:
            continue

        print(f"Found orphan cache file: {file_path.name}")
        print(f"  -> URL: {url}")
        
        try:
            print("  -> Fetching metadata from YouTube...")
            meta = fetch_metadata(url, download=False)
            title = meta.get("title") or ""
            uploader = meta.get("artist") or "" # uploader is usually returned as artist or can fallback
            duration = meta.get("duration")

            artist_clean, title_clean = parse_youtube_title(title, uploader)
            print(f"  -> Decoded clean metadata: '{artist_clean}' - '{title_clean}'")

            # Check if this track already exists in tracks table
            track_id = find_track_id_by_artist_title(artist_clean, title_clean, conn)
            if not track_id:
                # Create a new track entry
                cur.execute(
                    "INSERT INTO tracks (artist, title, duration) VALUES (?, ?, ?)",
                    (artist_clean, title_clean, duration)
                )
                track_id = cur.lastrowid
                print(f"  -> Created new track record (ID {track_id})")
            else:
                print(f"  -> Reusing existing track record (ID {track_id})")

            # Insert source mapping
            cur.execute(
                "INSERT INTO sources (track_id, kind, url, player_name) VALUES (?, 'youtube', ?, 'browser')",
                (track_id, url)
            )
            print("  -> Registered source mapping in DB")
            known_urls.add(url)

            # Analyze the local audio file now that it is mapped
            print("  -> Running audio analysis on local file...")
            result = analyze_audio(str(file_path))
            key = result.key
            
            kwargs = {
                "detected_key": key,
                "key_confidence": result.key_confidence,
                "key_agreement": result.key_agreement,
                "bpm": result.bpm,
                "method": result.method,
                "analyzer_version": result.version,
                "conn": conn,
            }
            if hasattr(result, "energy"):
                kwargs["energy"] = getattr(result, "energy", None)
            if hasattr(result, "brightness"):
                kwargs["brightness"] = getattr(result, "brightness", None)
                
            import inspect
            sig = inspect.signature(track_analysis.save_detected)
            for k in list(kwargs.keys()):
                if k not in sig.parameters:
                    del kwargs[k]
                    
            if track_id is not None:
                track_analysis.save_detected(int(track_id), **kwargs)
                print(f"  -> Key/BPM analyzed: key={key.name if key else 'unknown'}, bpm={result.bpm if result.bpm else 'unknown'}")
                healed_count += 1
        except Exception as e:
            print(f"  -> Failed to heal file: {e}")

    print(f"Orphan cache healing complete. Recovered {healed_count} file(s).")


def find_track_id_by_artist_title(artist: str, title: str, conn: sqlite3.Connection) -> Optional[int]:
    """Helper to lookup track_id case-insensitively."""
    cur = conn.cursor()
    cur.execute(
        "SELECT track_id FROM tracks WHERE lower(artist) = lower(?) AND lower(title) = lower(?)",
        (artist.strip(), title.strip())
    )
    row = cur.fetchone()
    return row[0] if row else None


def main() -> None:
    db_path = os.path.expanduser(settings.local_db)
    print(f"Using database: {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    track_analysis.ensure_schema(conn)

    try:
        with conn:
            # 1. Deduplication
            run_deduplication(conn)
            # 2. Source healing
            run_source_healing(conn, limit=30) # Let's heal a chunk of up to 30 tracks in this run
            # 3. Orphan cache healing
            run_orphan_cache_healing(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
