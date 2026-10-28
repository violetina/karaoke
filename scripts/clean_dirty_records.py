"""Data cleanup script for dirty artists and unsourced tracks in Karaoke database.

Performs:
1. Merging 38 unsourced duplicate tracks into their canonical sourced tracks.
2. Inverting Lastname, Firstname artist records (e.g. Tal Farlow, Erroll Garner, Lee Perry).
3. Swapping inverted Artist/Title records (e.g. Lou Levy - The Shadow of Your Smile).
4. Fixing truncated artist names and typos (e.g. Frank Zappa & The Mothers of Invention).
5. Extracting real artist and title from uploader-tagged tracks and compilations.
6. Pruning pure non-music / sound effects / dummy placeholder tracks.
7. Discovering and adding streaming sources (YouTube & Spotify) for remaining unsourced tracks.
8. Purging orphan artist_lookups and artist_genres.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from karaoke.config import settings
from karaoke.youtube import parse_youtube_title
from karaoke.ytmusic_client import YTMusicClient
from karaoke.spotify_client import SpotifyClient
from scripts.db_cleanup import are_titles_similar, are_artists_compatible, _norm_title
import psycopg
from psycopg.rows import dict_row

DEPENDENT_TABLES = (
    "lyrics",
    "sources",
    "track_analysis",
    "track_sync_offsets",
    "spotify_lookups",
    "restricted_tracks",
    "track_notes",
    "alignment_support",
    "track_art",
    "track_genre",
    "track_tone",
    "track_moods",
    "track_chords",
    "track_analysis_history",
    "sync_failures",
    "track_artists",
)

LASTNAME_FIRSTNAME_MAP = {
    "Farlow Tal": "Tal Farlow",
    "Garner Erroll": "Erroll Garner",
    "Perry, Lee": "Lee Perry",
    "Bellson Louis": "Louis Bellson",
    "Fuller Curtis": "Curtis Fuller",
    "Mehrstein Dino": "Dino Mehrstein",
    "Vaughan, Jimmie": "Jimmie Vaughan",
    "Walker, Errol": "Erroll Walker",
    "Wayne, Jon": "Jon Wayne",
    "Sinatra, Frank": "Frank Sinatra",
    "Cure, The": "The Cure",
    "Blasters, The": "The Blasters",
    "Hollies, The": "The Hollies",
    "Hombres, The": "The Hombres",
    "Mavericks, The": "The Mavericks",
    "Upsetter, The": "The Upsetter",
    "Bonfa, Luiz": "Luiz Bonfá",
    "Revell, Graeme": "Graeme Revell",
    "Sixteen, Earl": "Earl Sixteen",
    "Graham, Leo": "Leo Graham",
}

ARTIST_TYPO_MAP = {
    "Frank Zappa & The Mothers of I": "Frank Zappa & The Mothers of Invention",
    "The Descendanst": "Descendents",
    "The Racounteurs": "The Raconteurs",
    "The Strage Death Of Liberal England": "The Strange Death of Liberal England",
    "Vanessa Paradise": "Vanessa Paradis",
    "M.Davis & J.Coltrane - The Complete Columbia Recordings 1955-1961 (Box 6 Cd)": "Miles Davis & John Coltrane",
}

INVERTED_ARTIST_TITLE = [
    ("the shadow of your smile", "lou levy", "Lou Levy", "The Shadow of Your Smile"),
    ("The Shadow Of Your Smile", "George Benson", "George Benson", "The Shadow of Your Smile"),
    ("via con me", "paolo conte", "Paolo Conte", "Via con me"),
    ("what a difference a day made", "dinah washington", "Dinah Washington", "What a Difference a Day Made"),
    ("Why she could'nt come", "Svensson", "Esbjörn Svensson Trio", "Why She Couldn't Come"),
]

UPLOADER_TRACK_FIXES = {
    1110: ("Red Hot Chili Peppers", "Aeroplane"),
    1115: ("Bonnie \"Prince\" Billy", "Hard Life"),
    1593: ("Daevar", "Yellow Queen"),
    18059: ("Frank Sinatra", "It's A Blue World"),
    18061: ("Nine Inch Nails", "The Collector"),
    18093: ("Captain Beefheart", "My Head Is My Only House Unless It Rains"),
    18229: ("Fuck Buttons", "Mogwai Fear Satan"),
    18536: ("Venetian Snares", "Blister"),
    18583: ("Nine Inch Nails", "Starfuckers, Inc."),
    18617: ("New Beard", "I Walk the Streets"),
    18911: ("Iron Maiden", "Iron Maiden (Friday Rock Show Session 1979)"),
    18916: ("The Blasters", "Dark Night"),
    18918: ("Vulfpeck", "Baby I Don't Know Oh Oh (feat. Charles Jones)"),
    19144: ("Nina Simone", "Sinnerman"),
}


def merge_and_drop_track(cur, src_id: int, dest_id: int) -> None:
    """Migrate valuable metadata from src_id into dest_id, then delete src_id."""
    # 1. Sources: move any distinct sources
    cur.execute("SELECT url, kind, player_name FROM sources WHERE track_id = %s", (src_id,))
    for row in cur.fetchall():
        cur.execute("SELECT 1 FROM sources WHERE url = %s", (row["url"],))
        if not cur.fetchone():
            cur.execute("""
                INSERT INTO sources (track_id, kind, url, player_name)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (url) DO NOTHING
            """, (dest_id, row["kind"], row["url"], row["player_name"]))
    cur.execute("DELETE FROM sources WHERE track_id = %s", (src_id,))

    # 2. Lyrics: move if dest has no synced lyrics and src does
    cur.execute("SELECT lyric_id, kind, source, synced_lyrics, plain_lyrics FROM lyrics WHERE track_id = %s", (src_id,))
    for row in cur.fetchall():
        cur.execute("SELECT lyric_id, synced_lyrics, plain_lyrics FROM lyrics WHERE track_id = %s AND kind = %s", (dest_id, row["kind"]))
        dest_ly = cur.fetchone()
        if not dest_ly:
            cur.execute("UPDATE lyrics SET track_id = %s WHERE lyric_id = %s", (dest_id, row["lyric_id"]))
        elif row["synced_lyrics"] and not dest_ly["synced_lyrics"]:
            cur.execute("UPDATE lyrics SET source = %s, synced_lyrics = %s, plain_lyrics = %s WHERE lyric_id = %s",
                        (row["source"], row["synced_lyrics"], row["plain_lyrics"], dest_ly["lyric_id"]))

    # 3. Track Analysis: move if dest lacks bpm
    cur.execute("SELECT * FROM track_analysis WHERE track_id = %s", (src_id,))
    src_an = cur.fetchone()
    if src_an:
        cur.execute("SELECT * FROM track_analysis WHERE track_id = %s", (dest_id,))
        dest_an = cur.fetchone()
        if not dest_an:
            cur.execute("UPDATE track_analysis SET track_id = %s WHERE track_id = %s", (dest_id, src_id))
        elif src_an["bpm"] and not dest_an["bpm"]:
            cur.execute("""
                UPDATE track_analysis
                SET detected_key=%s, bpm=%s, energy=%s, brightness=%s, updated_at=%s
                WHERE track_id=%s
            """, (src_an["detected_key"], src_an["bpm"], src_an["energy"], src_an["brightness"], src_an["updated_at"], dest_id))

    # 4. Play count: accumulate
    cur.execute("SELECT play_count FROM tracks WHERE track_id = %s", (src_id,))
    src_row = cur.fetchone()
    if src_row and src_row["play_count"]:
        cur.execute("UPDATE tracks SET play_count = COALESCE(play_count, 0) + %s WHERE track_id = %s",
                    (src_row["play_count"], dest_id))

    # 5. Saved playlists and radio sessions
    cur.execute("UPDATE saved_playlist_tracks SET track_id = %s WHERE track_id = %s", (dest_id, src_id))
    cur.execute("DELETE FROM radio_session_tracks WHERE imported_track_id = %s", (src_id,))

    # 6. Delete src_id from all dependent tables and tracks
    for tbl in DEPENDENT_TABLES:
        cur.execute(f"DELETE FROM {tbl} WHERE track_id = %s", (src_id,))
    cur.execute("DELETE FROM tracks WHERE track_id = %s", (src_id,))


def update_track_artist_title(cur, track_id: int, new_art: str, new_tit: str) -> None:
    """Update artist and title on track_id, or merge into existing track if (artist, title) already exists."""
    cur.execute("SELECT track_id FROM tracks WHERE artist = %s AND title = %s", (new_art, new_tit))
    existing = cur.fetchone()
    if existing:
        if existing["track_id"] != track_id:
            print(f"    (artist, title) exists on track {existing['track_id']} -> merging {track_id} into {existing['track_id']}")
            merge_and_drop_track(cur, track_id, existing["track_id"])
    else:
        cur.execute("UPDATE tracks SET artist = %s, title = %s WHERE track_id = %s", (new_art, new_tit, track_id))


def prune_track(cur, tid: int) -> None:
    """Completely delete track and all associated child table rows."""
    cur.execute("DELETE FROM saved_playlist_tracks WHERE track_id = %s", (tid,))
    cur.execute("DELETE FROM radio_session_tracks WHERE imported_track_id = %s", (tid,))
    for tbl in DEPENDENT_TABLES:
        cur.execute(f"DELETE FROM {tbl} WHERE track_id = %s", (tid,))
    cur.execute("DELETE FROM tracks WHERE track_id = %s", (tid,))


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean dirty artists and unsourced records")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without committing")
    args = parser.parse_args()

    print(f"Starting database cleanup (dry_run={args.dry_run})...")

    with psycopg.connect(settings.db_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # ---------------------------------------------------------
            # Step 1: Merge unsourced duplicates into sourced tracks
            # ---------------------------------------------------------
            cur.execute("""
                SELECT track_id, artist, title, album, duration
                FROM tracks t
                WHERE NOT EXISTS (SELECT 1 FROM sources s WHERE s.track_id = t.track_id)
            """)
            unsourced = cur.fetchall()

            cur.execute("""
                SELECT track_id, artist, title, album, duration
                FROM tracks t
                WHERE EXISTS (SELECT 1 FROM sources s WHERE s.track_id = t.track_id)
            """)
            sourced = cur.fetchall()

            from collections import defaultdict
            index = defaultdict(list)
            for s in sourced:
                nt = _norm_title(s["title"])
                if nt:
                    index[nt.split()[0]].append(s)

            dups = []
            for u in unsourced:
                nt = _norm_title(u["title"])
                if not nt: continue
                candidates = index.get(nt.split()[0], [])
                for c in candidates:
                    if are_titles_similar(u["title"], c["title"], threshold=0.90) and are_artists_compatible(u["artist"], c["artist"]):
                        dups.append((u["track_id"], c["track_id"], u["artist"], u["title"], c["artist"], c["title"]))
                        break

            print(f"\n[1] Merging {len(dups)} duplicate unsourced tracks:")
            for src_id, dest_id, s_art, s_tit, d_art, d_tit in dups:
                print(f"  Merge {src_id} ('{s_art}' - '{s_tit}') -> {dest_id} ('{d_art}' - '{d_tit}')")
                if not args.dry_run:
                    merge_and_drop_track(cur, src_id, dest_id)

            # ---------------------------------------------------------
            # Step 2: Fix inverted Lastname, Firstname
            # ---------------------------------------------------------
            print(f"\n[2] Inverting Lastname, Firstname artist records:")
            for old_art, new_art in LASTNAME_FIRSTNAME_MAP.items():
                cur.execute("SELECT track_id, title FROM tracks WHERE artist = %s", (old_art,))
                tracks_to_update = cur.fetchall()
                if tracks_to_update:
                    print(f"  Updating '{old_art}' -> '{new_art}' ({len(tracks_to_update)} tracks)")
                    if not args.dry_run:
                        for tr in tracks_to_update:
                            update_track_artist_title(cur, tr["track_id"], new_art, tr["title"])

            # ---------------------------------------------------------
            # Step 3: Fix artist typos and truncated names
            # ---------------------------------------------------------
            print(f"\n[3] Fixing artist typos, truncated names & box sets:")
            for old_art, new_art in ARTIST_TYPO_MAP.items():
                cur.execute("SELECT track_id, title FROM tracks WHERE artist = %s", (old_art,))
                tracks_to_update = cur.fetchall()
                if tracks_to_update:
                    print(f"  Updating '{old_art}' -> '{new_art}' ({len(tracks_to_update)} tracks)")
                    if not args.dry_run:
                        for tr in tracks_to_update:
                            update_track_artist_title(cur, tr["track_id"], new_art, tr["title"])

            # ---------------------------------------------------------
            # Step 4: Fix inverted Artist / Title
            # ---------------------------------------------------------
            print(f"\n[4] Swapping inverted Artist/Title records:")
            for cur_art, cur_tit, new_art, new_tit in INVERTED_ARTIST_TITLE:
                cur.execute("SELECT track_id FROM tracks WHERE lower(artist) = lower(%s) AND lower(title) = lower(%s)",
                            (cur_art, cur_tit))
                rows = cur.fetchall()
                for r in rows:
                    print(f"  Track {r['track_id']}: '{cur_art}' / '{cur_tit}' -> '{new_art}' / '{new_tit}'")
                    if not args.dry_run:
                        update_track_artist_title(cur, r["track_id"], new_art, new_tit)

            # ---------------------------------------------------------
            # Step 5: Fix uploader-tagged tracks & compilations
            # ---------------------------------------------------------
            print(f"\n[5] Fixing uploader-tagged tracks & compilations:")
            for tid, (new_art, new_tit) in UPLOADER_TRACK_FIXES.items():
                cur.execute("SELECT track_id, artist, title FROM tracks WHERE track_id = %s", (tid,))
                row = cur.fetchone()
                if row:
                    print(f"  Track {tid}: '{row['artist']}' - '{row['title']}' -> '{new_art}' - '{new_tit}'")
                    if not args.dry_run:
                        update_track_artist_title(cur, tid, new_art, new_tit)

            # Fix GUITAR IDOLS tracks: title is "Artist - Title"
            cur.execute("SELECT track_id, title FROM tracks WHERE artist = 'GUITAR IDOLS'")
            for r in cur.fetchall():
                tit = r["title"]
                if " - " in tit:
                    parts = tit.split(" - ", 1)
                    g_art = re.sub(r"\s*\([^)]*\)", "", parts[0]).strip()
                    g_tit = parts[1].strip()
                    print(f"  GUITAR IDOLS Track {r['track_id']} -> '{g_art}' - '{g_tit}'")
                    if not args.dry_run:
                        update_track_artist_title(cur, r["track_id"], g_art, g_tit)

            # Fix Romantic Collection tracks
            cur.execute("SELECT track_id, title FROM tracks WHERE artist = 'Romantic Collection'")
            for r in cur.fetchall():
                tit = r["title"]
                m = re.match(r'^(.*?)\s*\"([^\"]*)\"?$', tit)
                if m:
                    r_art = m.group(1).strip()
                    r_tit = m.group(2).strip()
                    print(f"  Romantic Collection Track {r['track_id']} -> '{r_art}' - '{r_tit}'")
                    if not args.dry_run:
                        update_track_artist_title(cur, r["track_id"], r_art, r_tit)
                elif " - " in tit:
                    parts = tit.split(" - ", 1)
                    r_art = parts[0].strip()
                    r_tit = parts[1].strip()
                    print(f"  Romantic Collection Track {r['track_id']} -> '{r_art}' - '{r_tit}'")
                    if not args.dry_run:
                        update_track_artist_title(cur, r["track_id"], r_art, r_tit)

            # Fix Buddha-Bar tracks
            cur.execute("SELECT track_id, title FROM tracks WHERE artist = 'Buddha-Bar (CD Series)'")
            for r in cur.fetchall():
                tit = r["title"]
                if " - " in tit:
                    parts = tit.split(" - ", 1)
                    b_art = parts[0].strip()
                    b_tit = parts[1].strip()
                    print(f"  Buddha-Bar Track {r['track_id']} -> '{b_art}' - '{b_tit}'")
                    if not args.dry_run:
                        update_track_artist_title(cur, r["track_id"], b_art, b_tit)
                else:
                    m = re.match(r'^(.*?)\s*\(([^)]+)\)?$', tit)
                    if m:
                        b_art = m.group(2).strip()
                        b_tit = m.group(1).strip()
                        print(f"  Buddha-Bar Track {r['track_id']} -> '{b_art}' - '{b_tit}'")
                        if not args.dry_run:
                            update_track_artist_title(cur, r["track_id"], b_art, b_tit)

            # ---------------------------------------------------------
            # Step 6: Prune pure non-music junk & placeholders
            # ---------------------------------------------------------
            print(f"\n[6] Pruning pure non-music junk & placeholders:")
            cur.execute("""
                SELECT track_id, artist, title FROM tracks
                WHERE artist = '[no artist]'
                   OR (artist = 'artist' AND title ~ '^z-d-\\d+$')
                   OR artist IN ('02', '03', '04', '05', '06', '08')
                   OR artist = 'Inconnu'
                   OR (artist = 'Travolta & Samuel L. Jackson' AND title = 'Royale With Cheese')
                   OR (artist = 'Zappa about John & Yoko' AND title = 'KMET Radio Interview')
            """)
            junk_tracks = cur.fetchall()
            print(f"  Found {len(junk_tracks)} junk tracks to prune.")
            for r in junk_tracks:
                print(f"  Pruning track {r['track_id']}: '{r['artist']}' - '{r['title']}'")
                if not args.dry_run:
                    prune_track(cur, r["track_id"])

            # ---------------------------------------------------------
            # Step 7: Discover and add sources for remaining unsourced tracks
            # ---------------------------------------------------------
            cur.execute("""
                SELECT track_id, artist, title, album, duration
                FROM tracks t
                WHERE NOT EXISTS (SELECT 1 FROM sources s WHERE s.track_id = t.track_id)
                ORDER BY track_id
            """)
            remaining_unsourced = cur.fetchall()
            print(f"\n[7] Resolving streaming sources for {len(remaining_unsourced)} remaining unsourced tracks...")

            yt = YTMusicClient()
            sp = SpotifyClient()

            added_sources = 0
            dropped_unsourcable = 0

            for r in remaining_unsourced:
                tid = r["track_id"]
                art = r["artist"].strip()
                tit = r["title"].strip()

                yt_id = None
                try:
                    yt_id = yt.search_track(art, tit)
                except Exception:
                    pass

                sp_id = None
                try:
                    sp_id = sp.search_track(art, tit)
                except Exception:
                    pass

                if not yt_id and not sp_id:
                    print(f"  NO SOURCES on YT or Spotify -> Dropping track {tid}: '{art}' - '{tit}'")
                    if not args.dry_run:
                        prune_track(cur, tid)
                    dropped_unsourcable += 1
                else:
                    sources_to_add = []
                    if yt_id:
                        sources_to_add.append(("youtube", f"https://www.youtube.com/watch?v={yt_id}"))
                    if sp_id:
                        clean_id = sp_id.split(":")[-1]
                        sources_to_add.append(("spotify", f"https://open.spotify.com/track/{clean_id}"))

                    print(f"  Track {tid}: '{art}' - '{tit}' -> {len(sources_to_add)} source(s) found (YT:{bool(yt_id)}, SP:{bool(sp_id)})")
                    if not args.dry_run:
                        for kind, url in sources_to_add:
                            cur.execute("""
                                INSERT INTO sources (track_id, kind, url)
                                VALUES (%s, %s, %s)
                                ON CONFLICT DO NOTHING
                            """, (tid, kind, url))
                    added_sources += 1

            # ---------------------------------------------------------
            # Step 8: Purge orphan artist_lookups
            # ---------------------------------------------------------
            print(f"\n[8] Purging orphan artist_lookups:")
            if not args.dry_run:
                cur.execute("""
                    DELETE FROM artist_lookups al
                    WHERE NOT EXISTS (SELECT 1 FROM tracks t WHERE t.artist = al.artist_text)
                """)
                orphan_lookups = cur.rowcount
                print(f"  Purged {orphan_lookups} orphan artist_lookups.")

            print(f"\nSummary:")
            print(f"  Merged duplicates: {len(dups)}")
            print(f"  Pruned junk: {len(junk_tracks)}")
            print(f"  Dropped unsourcable: {dropped_unsourcable}")
            print(f"  Sourced tracks: {added_sources}")

            if not args.dry_run:
                conn.commit()
                print("Transaction successfully committed to Postgres.")
            else:
                conn.rollback()
                print("Dry-run complete. No changes made.")


if __name__ == "__main__":
    main()
