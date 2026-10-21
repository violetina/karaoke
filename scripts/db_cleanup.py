"""Data cleanup, deduplication, and self-healing script for the karaoke platform database."""
from __future__ import annotations

import argparse
import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

from karaoke.config import settings
from karaoke.lyrics import clean_title
from karaoke.youtube import fetch_metadata, parse_youtube_title, search
from karaoke import localcache, track_analysis

# Two tracks whose durations differ by <= this many seconds are considered the
# "same length" (same song); a larger gap means a different version (edit, live,
# extended, remix) and the tracks are allowed to coexist.
DURATION_TOLERANCE_S = 4.0

# Minimum fuzzy similarity (0..1) on the cleaned title for two tracks to be
# considered the same song title.
TITLE_SIMILARITY = 0.86

# When neither track has a known duration we cannot confirm "same length", so we
# only merge on a near-identical title to stay safe.
TITLE_SIMILARITY_NO_DURATION = 0.97


def _norm_title(title: str) -> str:
    """Normalized, decoration-stripped title for fuzzy comparison."""
    return clean_title(title or "").lower().strip()


def are_titles_similar(t1: str, t2: str, threshold: float = TITLE_SIMILARITY) -> bool:
    """True if two titles are fuzzily the same song (after stripping decorations)."""
    n1, n2 = _norm_title(t1), _norm_title(t2)
    if not n1 or not n2:
        return False
    if n1 == n2:
        return True
    return SequenceMatcher(None, n1, n2).ratio() >= threshold


# Words that mark a *different recording* rather than a different spelling.
# A demo, a live take and a remaster are separate performances: they have their
# own lengths, their own timings, and often their own words. Merging them
# attaches one version's synced lyrics to another.
_VERSION_MARKER = re.compile(
    r"\b(demos?|live|unplugged|acoustic|instrumental|a\s?cappella|"
    r"remaster(?:ed)?|remix(?:es)?|rough\s*mix|alternate|alternative\s+take|"
    r"rehearsal|session|radio\s*edit|single\s+version|reprise|"
    r"mono|karaoke|cover|edit)\b",
    re.IGNORECASE,
)


def version_markers(title: str) -> frozenset:
    """Version words present in a title, lowercased.

    Compared as sets rather than tested for presence: two tracks both marked
    "(Live)" are the same live recording under different spellings and should
    still merge. It is a *difference* in markers that means different takes.
    """
    return frozenset(m.group(0).lower().replace(" ", "")
                     for m in _VERSION_MARKER.finditer(title or ""))


def same_version(t1: str, t2: str) -> bool:
    """Whether two titles describe the same recording."""
    return version_markers(t1) == version_markers(t2)


def duration_relation(d1: Optional[float], d2: Optional[float]) -> str:
    """Classify two durations: 'same', 'different', or 'unknown'.

    'same'      -> within DURATION_TOLERANCE_S (same length == same song)
    'different' -> known but far apart (a different version; allowed to coexist)
    'unknown'   -> at least one duration is missing
    """
    if d1 is None or d2 is None:
        return "unknown"
    return "same" if abs(float(d1) - float(d2)) <= DURATION_TOLERANCE_S else "different"


_ROMAN_MAP = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5,
    "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
    "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15,
}


def normalize_part_num(val: str) -> str:
    """Normalize digits or Roman numerals to a stringified integer."""
    val = val.lower().strip()
    if val in _ROMAN_MAP:
        return str(_ROMAN_MAP[val])
    if val.isdigit():
        return str(int(val))
    return val


def extract_part_numbers(title: str) -> set[str]:
    """Extract semantic part, volume, movement, or catalogue indicators from a title."""
    t = (title or "").lower()
    # Strip release years in parentheses like '(2008)'
    t = re.sub(r"\(\s*(?:19|20)\d{2}\s*\)", "", t)
    parts = set()

    # Leading number before title (e.g. '05 - Song' or '5 Ghosts')
    m_lead = re.match(r"^\s*(\d+)(?:\s*[-–—.]|\s+)", t)
    if m_lead:
        parts.add(f"lead_{normalize_part_num(m_lead.group(1))}")

    # Strip track number prefixes like '01 - ' or '01. '
    t = re.sub(r"^\s*\d{1,3}\s*[-–—.]\s*", "", t)

    # Explicit part / pt / no / volume / vol / act / movement / suite / scene
    for m in re.finditer(r"\b(?:pt\.?|part|no\.?|n°|volume|vol\.?|act|movement|suite|scene)\s*[,.:-]?\s*([0-9]+|[ivxlcdm]+)\b", t):
        parts.add(f"part_{normalize_part_num(m.group(1))}")

    # #\d+ (e.g. Untitled #3 vs Untitled #11)
    for m in re.finditer(r"#\s*(\d+)", t):
        parts.add(f"hash_{normalize_part_num(m.group(1))}")

    # Trailing digits or roman numerals at end of title (e.g. Pigs On The Wing 1 vs 2, Ghosts I vs II)
    trail = re.search(r"\b([0-9]+|[ivxlcdm]+)\s*(?:\([^)]*\))?\s*$", t)
    if trail:
        tok = trail.group(1)
        parts.add(f"trail_{normalize_part_num(tok)}")

    # Sub-index / catalogue numbers e.g. Seb-1.06 vs Seb-1.09
    for m in re.finditer(r"\b\d+\.(\d+)\b", t):
        parts.add(f"sub_{normalize_part_num(m.group(1))}")

    return parts


def compatible_parts(t1: str, t2: str) -> bool:
    """True if titles do not have conflicting part/track/index numbers."""
    p1 = extract_part_numbers(t1)
    p2 = extract_part_numbers(t2)
    for pfx in ("part_", "hash_", "trail_", "sub_", "lead_"):
        vals1 = {x for x in p1 if x.startswith(pfx)}
        vals2 = {x for x in p2 if x.startswith(pfx)}
        if vals1 and vals2 and vals1 != vals2:
            return False
    return True


def is_duplicate(t1: dict[str, Any], t2: dict[str, Any]) -> bool:
    """Decide whether two tracks represent the same song and should be merged.

    Rule (per user intent): same/compatible artist AND a matching title, where a
    *different video with the same length* counts as a duplicate, but a
    *different length* is a distinct version and is left alone. When durations
    are unknown we require a near-identical title before merging.
    """
    if not are_artists_compatible(t1["artist"], t2["artist"]):
        return False
    # Checked before the duration relation, and independently of it: a live
    # take can happen to run the same length as the studio cut, so "same
    # length" is not evidence that they are the same recording.
    if not same_version(t1["title"], t2["title"]):
        return False
    # Ensure distinct parts/suites/numbers are never falsely merged together.
    if not compatible_parts(t1["title"], t2["title"]):
        return False
    rel = duration_relation(t1["duration"], t2["duration"])
    if rel == "same":
        return are_titles_similar(t1["title"], t2["title"])
    if rel == "different":
        return False  # different version — allowed to coexist
    # unknown duration: only merge on a near-exact title
    return are_titles_similar(t1["title"], t2["title"], TITLE_SIMILARITY_NO_DURATION)



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


def merge_tracks(track_id_src: int, track_id_dest: int, conn) -> None:
    """Merge Track A (src) into Track B (dest)."""
    cur = conn.cursor()

    # Move sources (deduplicating URLs)
    cur.execute("SELECT url, kind, player_name FROM sources WHERE track_id = %s", (track_id_src,))
    src_sources = cur.fetchall()
    for row in src_sources:
        url, kind, player_name = row["url"], row["kind"], row["player_name"]
        # Check if destination already has this URL
        cur.execute("SELECT source_id FROM sources WHERE track_id = %s AND url = %s", (track_id_dest, url))
        if cur.fetchone():
            # Delete duplicate from source track
            cur.execute("DELETE FROM sources WHERE track_id = %s AND url = %s", (track_id_src, url))
        else:
            # Move source
            cur.execute("UPDATE sources SET track_id = %s WHERE track_id = %s AND url = %s", (track_id_dest, track_id_src, url))

    # Move lyrics (keeping the better ones)
    cur.execute("SELECT lyric_id, kind, source, synced_lyrics, plain_lyrics FROM lyrics WHERE track_id = %s", (track_id_src,))
    src_lyrics = cur.fetchall()
    for row in src_lyrics:
        ly_id, kind, source, synced, plain = row["lyric_id"], row["kind"], row["source"], row["synced_lyrics"], row["plain_lyrics"]
        # Check if destination already has lyrics of this kind
        cur.execute("SELECT lyric_id, synced_lyrics, plain_lyrics FROM lyrics WHERE track_id = %s AND kind = %s", (track_id_dest, kind))
        dest_ly = cur.fetchone()
        if dest_ly:
            # Determine which is better (synced over plain)
            dest_has_synced = bool(dest_ly["synced_lyrics"])
            src_has_synced = bool(synced)
            if src_has_synced and not dest_has_synced:
                # Replace dest lyrics with src lyrics
                cur.execute("UPDATE lyrics SET source = %s, synced_lyrics = %s, plain_lyrics = %s WHERE lyric_id = %s", 
                            (source, synced, plain, dest_ly["lyric_id"]))
            # Delete the source lyrics row
            cur.execute("DELETE FROM lyrics WHERE lyric_id = %s", (ly_id,))
        else:
            # Move lyrics
            cur.execute("UPDATE lyrics SET track_id = %s WHERE lyric_id = %s", (track_id_dest, ly_id))

    # Move track analysis (keeping the more complete one)
    cur.execute("SELECT * FROM track_analysis WHERE track_id = %s", (track_id_src,))
    src_analysis = cur.fetchone()
    if src_analysis:
        cur.execute("SELECT * FROM track_analysis WHERE track_id = %s", (track_id_dest,))
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
                    SET detected_key=%s, key_confidence=%s, key_agreement=%s, reference_key=%s, reference_src=%s,
                        resolved_key=%s, key_relation=%s, bpm=%s, method=%s, analyzer_version=%s, updated_at=%s,
                        energy=%s, brightness=%s
                    WHERE track_id=%s
                    """,
                    (src_analysis["detected_key"], src_analysis["key_confidence"], src_analysis["key_agreement"],
                     src_analysis["reference_key"], src_analysis["reference_src"], src_analysis["resolved_key"],
                     src_analysis["key_relation"], src_analysis["bpm"], src_analysis["method"],
                     src_analysis["analyzer_version"], src_analysis["updated_at"], src_analysis["energy"],
                     src_analysis["brightness"], track_id_dest)
                )
            cur.execute("DELETE FROM track_analysis WHERE track_id = %s", (track_id_src,))
        else:
            cur.execute("UPDATE track_analysis SET track_id = %s WHERE track_id = %s", (track_id_dest, track_id_src))

    # Delete the source track row from tracks
    cur.execute("DELETE FROM tracks WHERE track_id = %s", (track_id_src,))


def run_deduplication(conn, dry_run: bool = False) -> int:
    """Scan and merge duplicate tracks representing the same song.

    Two tracks are duplicates when the artist is compatible and the title
    fuzzily matches AND the durations agree (same length). Different lengths are
    treated as distinct versions and left in place (a song may legitimately have
    several videos). Returns the number of merges performed (or would perform in
    dry-run).
    """
    from collections import defaultdict

    print("=== Step 1: Track Deduplication and Merging ===")
    if dry_run:
        print("(dry-run: no changes will be written)")
    cur = conn.cursor()
    cur.execute("SELECT track_id, artist, title, duration FROM tracks ORDER BY track_id")
    tracks = cur.fetchall()

    # Pre-fetch lyrics presence and source counts to avoid per-pair DB roundtrips
    cur.execute("SELECT track_id FROM lyrics WHERE COALESCE(synced_lyrics,'') || COALESCE(plain_lyrics,'') != ''")
    has_lyrics_ids = {row["track_id"] for row in cur.fetchall()}
    cur.execute("SELECT track_id, count(*) as cnt FROM sources GROUP BY track_id")
    source_counts = {row["track_id"]: row["cnt"] for row in cur.fetchall()}

    def _score(tid: int) -> tuple[int, int]:
        return (1 if tid in has_lyrics_ids else 0, source_counts.get(tid, 0))

    def _pick_canonical(t1: dict, t2: dict):
        s1, s2 = _score(t1["track_id"]), _score(t2["track_id"])
        if s1 != s2:
            return (t1, t2) if s1 > s2 else (t2, t1)
        return (t1, t2) if len(t1["artist"]) >= len(t2["artist"]) else (t2, t1)

    # Group tracks by normalized artist to avoid O(N^2) full table scans
    def _artist_group_key(a: str) -> str:
        s = (a or "").lower().strip()
        s = re.sub(r"^(?:the|a|an)\s+", "", s)
        s = re.sub(r"[\(\)\[\]\-\&\,\s]+", " ", s).strip()
        return s

    groups = defaultdict(list)
    for t in tracks:
        k = _artist_group_key(t["artist"])
        if k and not k.startswith("various"):
            groups[k].append(t)

    # Track which ids have been merged away so we don't reuse them.
    merged_away: set[int] = set()
    merged_count = 0

    for k, grp in groups.items():
        if len(grp) < 2:
            continue
        for i in range(len(grp)):
            t1 = grp[i]
            if t1["track_id"] in merged_away:
                continue
            for j in range(i + 1, len(grp)):
                t2 = grp[j]
                if t2["track_id"] in merged_away:
                    continue
                if not is_duplicate(t1, t2):
                    continue

                dest, src = _pick_canonical(t1, t2)

                d1, d2 = t1["duration"], t2["duration"]
                dd = "?" if (d1 is None or d2 is None) else f"{abs(d1 - d2):.0f}s"
                print(f"Duplicate (Δdur={dd}): '{src['artist']}' - '{src['title']}' (ID {src['track_id']})")
                print(f"              merge into: '{dest['artist']}' - '{dest['title']}' (ID {dest['track_id']})")

                if not dry_run:
                    merge_tracks(src["track_id"], dest["track_id"], conn)
                    source_counts[dest["track_id"]] = source_counts.get(dest["track_id"], 0) + source_counts.get(src["track_id"], 0)
                    if src["track_id"] in has_lyrics_ids:
                        has_lyrics_ids.add(dest["track_id"])

                merged_away.add(src["track_id"])
                merged_count += 1
                if src["track_id"] == t1["track_id"]:
                    break  # t1 was merged away; move to next i

    verb = "Would merge" if dry_run else "Merged"
    print(f"Deduplication complete. {verb} {merged_count} duplicate track(s).")
    return merged_count



def run_source_healing(conn, limit: int = 50) -> None:
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
            cur.execute("DELETE FROM tracks WHERE track_id = %s", (r["track_id"],))
            cur.execute("DELETE FROM lyrics WHERE track_id = %s", (r["track_id"],))
            cur.execute("DELETE FROM track_analysis WHERE track_id = %s", (r["track_id"],))
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
                    "INSERT INTO sources (track_id, kind, url, player_name) VALUES (%s, %s, %s, %s)",
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


def run_orphan_cache_healing(conn) -> None:
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
                # Create a new track entry and retrieve the auto-generated ID
                cur.execute(
                    "INSERT INTO tracks (artist, title, duration) VALUES (%s, %s, %s) RETURNING track_id",
                    (artist_clean, title_clean, duration)
                )
                row = cur.fetchone()
                track_id = row["track_id"]
                print(f"  -> Created new track record (ID {track_id})")
            else:
                print(f"  -> Reusing existing track record (ID {track_id})")

            # Insert source mapping
            cur.execute(
                "INSERT INTO sources (track_id, kind, url, player_name) VALUES (%s, 'youtube', %s, 'browser')",
                (track_id, url)
            )
            print("  -> Registered source mapping in DB")
            known_urls.add(url)

            # Analyze the local audio file now that it is mapped
            from karaoke.analyze import analyze_audio

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


def find_track_id_by_artist_title(artist: str, title: str, conn) -> Optional[int]:
    """Helper to lookup track_id case-insensitively."""
    cur = conn.cursor()
    cur.execute(
        "SELECT track_id FROM tracks WHERE lower(artist) = lower(%s) AND lower(title) = lower(%s)",
        (artist.strip(), title.strip())
    )
    row = cur.fetchone()
    return row["track_id"] if row else None


def fix_doubled_lyrics(conn, dry_run: bool = False) -> int:
    """Find and collapse lyrics whose plain or synced text was duplicated wholesale."""
    from karaoke.ytmusic_lyrics import _undouble

    def undouble_synced(synced: str) -> str:
        if not synced:
            return synced
        lines = synced.splitlines()
        parsed = []
        for l in lines:
            m = re.match(r'^(\[\d+:\d+(?:\.\d+)?\]|\<\d+:\d+(?:\.\d+)?\>)(.*)$', l.strip())
            if m:
                parsed.append((m.group(1), m.group(2)))
            else:
                parsed.append(('', l))
        
        text_lines = [p[1].strip() for p in parsed if p[1].strip()]
        if not text_lines:
            return synced
        
        undoubled_text = _undouble(text_lines)
        if len(undoubled_text) < len(text_lines):
            n = len(undoubled_text)
            kept_parsed = []
            count = 0
            for prefix, text in parsed:
                if text.strip():
                    if count < n:
                        kept_parsed.append(f'{prefix}{text}')
                        count += 1
                else:
                    if count <= n:
                        kept_parsed.append(f'{prefix}{text}')
            return '\n'.join(kept_parsed)
        return synced

    def undouble_plain(plain: str) -> str:
        if not plain:
            return plain
        lines = plain.splitlines()
        text_lines = [l.strip() for l in lines if l.strip()]
        if not text_lines:
            return plain
        undoubled_text = _undouble(text_lines)
        if len(undoubled_text) < len(text_lines):
            return '\n'.join(undoubled_text)
        return plain

    cur = conn.cursor()
    cur.execute("SELECT lyric_id, track_id, plain_lyrics, synced_lyrics FROM lyrics WHERE kind = 'approved'")
    rows = cur.fetchall()

    fixed = 0
    for r in rows:
        p_orig = r['plain_lyrics'] or ''
        s_orig = r['synced_lyrics'] or ''
        p_new = undouble_plain(p_orig)
        s_new = undouble_synced(s_orig)
        if p_new != p_orig or s_new != s_orig:
            fixed += 1
            if not dry_run:
                cur.execute(
                    "UPDATE lyrics SET plain_lyrics = %s, synced_lyrics = %s WHERE lyric_id = %s",
                    (p_new, s_new, r['lyric_id'])
                )
    print(f"Fixed {fixed} track(s) with doubled lyrics.")
    return fixed


def prune_junk_tracks(conn, dry_run: bool = False) -> int:
    """Delete non-song rows (browser tabs, idle placeholders, empty-artist commands)."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT track_id, artist, title
        FROM tracks
        WHERE (lower(artist) = 'karaoke' AND lower(title) = 'idle')
           OR lower(title) LIKE '%%het archief voor onderwijs%%'
           OR (COALESCE(artist, '') = '' AND lower(title) IN ('print', 'help', 'stage', 'idle', 'unknown'))
        ORDER BY track_id
        """
    )
    rows = cur.fetchall()
    for r in rows:
        tid = r["track_id"]
        print(f"Pruning non-song track ID {tid}: '{r['artist']}' - '{r['title']}'")
        if not dry_run:
            for tbl in (
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
                "radio_session_tracks",
                "sync_failures",
            ):
                try:
                    cur.execute(f"DELETE FROM {tbl} WHERE track_id = %s", (tid,))
                except Exception:
                    pass
            cur.execute("DELETE FROM tracks WHERE track_id = %s", (tid,))
    if not dry_run:
        cur.execute(
            """
            DELETE FROM lyric_gaps
            WHERE (lower(artist) = 'karaoke' AND lower(title) = 'idle')
               OR lower(title) LIKE '%%het archief voor onderwijs%%'
            """
        )
    print(f"Pruned {len(rows)} non-song track(s).")
    return len(rows)


def audit_and_fix_messed_up_lyrics(
    conn,
    *,
    dry_run: bool = False,
    fetch_online: bool = False,
    enqueue_sync: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Detect and repair tracks with mismatched or poorly aligned synced lyrics.

    Categories detected:
    - ``overrun``: last LRC timestamp runs > 10s past ``tracks.duration``.
    - ``edition_clash``: identical ``synced_lyrics`` text shared across multiple
      tracks whose durations differ by > 25s, where this track's duration
      disagrees with the LRC span (last_t > dur + 5s or dur - last_t > 45s).
    - ``poor_alignment``: Whisper alignment with >= 8 lines where < 25% of lines
      anchored on a heard word.

    When ``not dry_run``, clears the mismatched ``synced_lyrics`` (preserving
    ``plain_lyrics`` so Whisper can re-align against the track's actual audio),
    optionally attempts a duration-validated LRCLIB lookup first
    (``fetch_online=True``), and optionally enqueues tracks with downloadable
    audio for background Whisper sync (``enqueue_sync=True``).
    """
    from concurrent.futures import ThreadPoolExecutor
    import hashlib
    from karaoke.lyrics import fetch_lrclib, parse_lrc
    from karaoke.postprocess_queue import (
        enqueue_if_needed,
        has_downloadable_source,
    )

    cur = conn.cursor()
    cur.execute(
        """
        SELECT t.track_id, t.artist, t.title, t.album, t.duration,
               l.lyric_id, l.source, l.synced_lyrics, l.plain_lyrics,
               a.lines AS sup_lines, a.anchored AS sup_anchored,
               a.longest_gap_s, a.unanchored_fraction
        FROM tracks t
        JOIN lyrics l ON l.track_id = t.track_id AND l.kind = 'approved'
        LEFT JOIN alignment_support a ON a.track_id = t.track_id
        WHERE COALESCE(l.synced_lyrics, '') != ''
        ORDER BY t.track_id
        """
    )
    rows = cur.fetchall()

    by_lrc: dict[str, list[dict[str, Any]]] = {}
    parsed_rows: list[dict[str, Any]] = []
    for r in rows:
        lines = parse_lrc(r["synced_lyrics"])
        if not lines:
            continue
        item = dict(r)
        item["last_t"] = lines[-1][0]
        item["n_lines"] = len(lines)
        parsed_rows.append(item)
        h = hashlib.md5(r["synced_lyrics"].strip().encode()).hexdigest()
        by_lrc.setdefault(h, []).append(item)

    clash_ids: set[int] = set()
    edition_clashes: list[dict[str, Any]] = []
    for h, grp in by_lrc.items():
        if len(grp) < 2:
            continue
        durs = [float(x["duration"]) for x in grp if x["duration"] and float(x["duration"]) > 30]
        if len(durs) < 2 or (max(durs) - min(durs)) <= 25.0:
            continue
        for x in grp:
            dur = float(x["duration"]) if x["duration"] else None
            last_t = x["last_t"]
            if dur and (last_t > dur + 5.0 or (dur - last_t) > 45.0):
                clash_ids.add(int(x["track_id"]))
                edition_clashes.append(x)

    overruns: list[dict[str, Any]] = []
    poor_alignments: list[dict[str, Any]] = []

    for x in parsed_rows:
        tid = int(x["track_id"])
        dur = float(x["duration"]) if x["duration"] else None
        if dur and 30.0 < dur < 1200.0 and x["last_t"] > dur + 10.0 and tid not in clash_ids:
            overruns.append(x)
        if x.get("sup_lines") and int(x["sup_lines"]) >= 8:
            ratio = float(x.get("sup_anchored") or 0) / int(x["sup_lines"])
            if ratio < 0.25:
                poor_alignments.append(x)

    all_flagged: dict[int, tuple[str, dict[str, Any]]] = {}
    for x in edition_clashes:
        all_flagged[int(x["track_id"])] = ("edition_clash", x)
    for x in overruns:
        all_flagged.setdefault(int(x["track_id"]), ("overrun", x))
    for x in poor_alignments:
        all_flagged.setdefault(int(x["track_id"]), ("poor_alignment", x))

    print(
        f"Lyrics audit found {len(all_flagged)} messed-up track(s): "
        f"{len(edition_clashes)} edition clashes, {len(overruns)} timestamp overruns, "
        f"{len(poor_alignments)} poor Whisper alignments (<25% anchored)."
    )

    if dry_run or not all_flagged:
        return {
            "edition_clash": edition_clashes,
            "overrun": overruns,
            "poor_alignment": poor_alignments,
        }

    online_results: dict[int, Any] = {}
    if fetch_online:
        candidates = [
            (tid, x)
            for tid, (reason, x) in sorted(all_flagged.items())
            if reason in ("edition_clash", "overrun") and x.get("duration")
        ]

        def _lookup(item: tuple[int, dict[str, Any]]) -> tuple[int, Any]:
            tid_i, row_i = item
            try:
                res = fetch_lrclib(
                    row_i["artist"] or "",
                    row_i["title"] or "",
                    album=row_i["album"] or "",
                    duration=float(row_i["duration"]),
                    timeout=5.0,
                )
                return tid_i, res
            except Exception:
                return tid_i, None

        if len(candidates) <= 1:
            for item in candidates:
                k, v = _lookup(item)
                online_results[k] = v
        else:
            with ThreadPoolExecutor(max_workers=8) as pool:
                for k, v in pool.map(_lookup, candidates):
                    online_results[k] = v

    upgraded_online = 0
    cleared_to_plain = 0
    enqueued = 0

    for tid, (reason, x) in sorted(all_flagged.items()):
        artist = x["artist"] or ""
        title = x["title"] or ""
        plain = (x["plain_lyrics"] or "").strip()
        if not plain and x["synced_lyrics"]:
            plain = "\n".join(txt for _, txt in parse_lrc(x["synced_lyrics"]))

        replaced = False
        ly = online_results.get(tid)
        if ly is not None:
            if ly.has_synced:
                cur.execute(
                    "UPDATE lyrics SET synced_lyrics = %s, plain_lyrics = COALESCE(NULLIF(%s, ''), plain_lyrics), source = 'lrclib' WHERE lyric_id = %s",
                    (ly.synced_raw, ly.plain, x["lyric_id"]),
                )
                cur.execute("DELETE FROM alignment_support WHERE track_id = %s", (tid,))
                try:
                    cur.execute("DELETE FROM sync_failures WHERE track_id = %s", (tid,))
                except Exception:
                    pass
                upgraded_online += 1
                replaced = True
            elif ly.plain and len(ly.plain) > len(plain):
                plain = ly.plain

        if not replaced:
            cur.execute(
                "UPDATE lyrics SET synced_lyrics = '', plain_lyrics = %s, source = CASE WHEN source = 'no_youtube_captions' THEN 'lrclib' ELSE source END WHERE lyric_id = %s",
                (plain, x["lyric_id"]),
            )
            cur.execute("DELETE FROM alignment_support WHERE track_id = %s", (tid,))
            try:
                cur.execute("DELETE FROM sync_failures WHERE track_id = %s", (tid,))
            except Exception:
                pass
            cleared_to_plain += 1
            if enqueue_sync and has_downloadable_source(tid, conn):
                if enqueue_if_needed(artist, title, conn=conn):
                    enqueued += 1

    conn.commit()
    print(
        f"Lyrics repair complete: {upgraded_online} replaced with duration-matched LRCLIB, "
        f"{cleared_to_plain} reset to plain text for Whisper re-alignment ({enqueued} enqueued)."
    )
    return {
        "edition_clash": edition_clashes,
        "overrun": overruns,
        "poor_alignment": poor_alignments,
    }


def repair_harmony_styles(conn, dry_run: bool = False) -> int:
    """Recompute track_chords harmony styles under updated _style() rules."""
    from psycopg.types.json import Jsonb
    from karaoke import harmony

    cur = conn.cursor()
    style_updates = 0
    if localcache.table_exists(conn, "track_chords"):
        cur.execute("SELECT track_id, harmony FROM track_chords")
        for r in cur.fetchall():
            h = dict(r["harmony"] or {})
            if not h or "key_tonic" not in h:
                continue
            summary = harmony.HarmonySummary(
                key_tonic=int(h.get("key_tonic", 0)),
                key_minor=bool(h.get("key_minor", False)),
                ii_v_i=int(h.get("ii_v_i", 0)),
                minor_ii_v_i=int(h.get("minor_ii_v_i", 0)),
                ii_v=int(h.get("ii_v", 0)),
                tritone_subs=int(h.get("tritone_subs", 0)),
                secondary_dominants=int(h.get("secondary_dominants", 0)),
                backdoor=int(h.get("backdoor", 0)),
                andalusian=int(h.get("andalusian", 0)),
                dominant_share=float(h.get("dominant_share", 0.0)),
                blues_score=float(h.get("blues_score", 0.0)),
                cadences_per_minute=float(h.get("cadences_per_minute", 0.0)),
                fifth_chains_per_minute=float(h.get("fifth_chains_per_minute", 0.0)),
            )
            new_style = harmony._style(summary)
            if new_style != h.get("style"):
                style_updates += 1
                if not dry_run:
                    h["style"] = new_style
                    cur.execute(
                        "UPDATE track_chords SET harmony = %s WHERE track_id = %s",
                        (Jsonb(h), r["track_id"]),
                    )

    if not dry_run and style_updates:
        conn.commit()
    return style_updates


def main() -> None:
    parser = argparse.ArgumentParser(description="Karaoke DB cleanup / dedup / self-healing")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report duplicates without merging or healing.")
    parser.add_argument("--dedup-only", action="store_true",
                        help="Only run deduplication (skip source/cache healing).")
    parser.add_argument("--fix-lyrics", action="store_true",
                        help="Audit and fix mismatched synced lyrics, edition clashes, and poor alignments.")
    parser.add_argument("--enqueue-sync", action="store_true",
                        help="Enqueue repaired tracks for background Whisper re-alignment.")
    args = parser.parse_args()

    print("Using database: PostgreSQL (via localcache.connect())")
    conn = localcache.connect()
    track_analysis.ensure_schema(conn)

    try:
        if args.dry_run:
            # Read-only: report duplicates, touch nothing.
            prune_junk_tracks(conn, dry_run=True)
            run_deduplication(conn, dry_run=True)
            fix_doubled_lyrics(conn, dry_run=True)
            audit_and_fix_messed_up_lyrics(conn, dry_run=True)
            return
        # 0. Prune non-song browser tabs and idle placeholders
        prune_junk_tracks(conn)
        # 1. Deduplication & Lyrics Undoubling
        run_deduplication(conn)
        fix_doubled_lyrics(conn)
        if args.fix_lyrics:
            audit_and_fix_messed_up_lyrics(
                conn,
                fetch_online=True,
                enqueue_sync=args.enqueue_sync,
            )
        conn.commit()
        if not args.dedup_only:
            # 2. Source healing
            run_source_healing(conn, limit=30)  # heal up to 30 tracks per run
            conn.commit()
            # 3. Orphan cache healing
            run_orphan_cache_healing(conn)
            conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()

