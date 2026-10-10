# Database Cleanup, Deduplication, and Self-Healing

This document guides you on how to maintain database consistency, resolve duplicate tracks, auto-fill missing source URLs, and heal orphan cached audio files.

## 1. The Duplicate Track Problem (disjoint records)

Discrepancies in artist name formatting between media players (Spotify/MPRIS), YouTube uploader strings, and lyric databases (LRCLIB/Genius) often create duplicate track entries in the `tracks` table.
* **Example:**
  - Track A: `"Inpatient"` - `"Neurodivergent"` (has Spotify source URL but no lyrics)
  - Track B: `"Inpatient, Ren & Chris Webby"` - `"Neurodivergent"` (has approved lyrics, but no sources)
* **Impact:** Playing the song on Spotify resolves to Track A (no lyrics) while in Browse mode you see Track B with lyrics.

### Mitigation: Fuzzy Deduplication & Merging
To resolve this, group tracks by lowercase title and compare artists with fuzzy similarity (checking if one artist is a substring of the other, or if they share the first artist name after stripping `- Topic` and punctuation).

When merging duplicate Track A (source) into Track B (destination):
1. **Move Sources:** `UPDATE sources SET track_id = B WHERE track_id = A`. Delete any duplicate URLs on B.
2. **Move Lyrics:** `UPDATE lyrics SET track_id = B WHERE track_id = A`. Keep synced lyrics over plain lyrics if both exist, and delete duplicate rows.
3. **Move Analysis:** `UPDATE track_analysis SET track_id = B WHERE track_id = A`. Keep the row with the most complete key/BPM/energy/brightness.
4. **Delete Duplicate Track:** `DELETE FROM tracks WHERE track_id = A`.

---

## 2. Auto-Filling Missing Source URLs (Orphan Tracks)

Tracks might have approved lyrics (e.g. from bulk imports) but no entries in the `sources` table, rendering them un-openable on Enter.
* **Mitigation:** Query the database for tracks with lyrics but no source rows. Exclude obvious test/mock entries (like `"analyse"` or `"tui"`). For valid tracks, search YouTube for `"{artist} {title}"` and automatically register the best match webpage URL as a `youtube` source with `player_name = 'browser'`.
* **Throttling:** Pause for 1.0-2.0s between searches to prevent YouTube rate limits (429).

---

## 3. Self-Healing Orphan Cached Audio Files

Sometimes, downloaded cache files (`*.webm` under `~/.local/share/karaoke/youtube/`) exist, but their video IDs are missing in SQLite.
* **Mitigation:** Scan the local directory for cache files, construct their YouTube URLs (`https://www.youtube.com/watch?v={video_id}`), and check if they exist in `sources`.
* If missing:
  1. Call `yt_dlp` to extract webpage metadata (Title, Uploader, etc.).
  2. Parse the clean artist/title via `parse_youtube_title(title, uploader)`.
  3. Insert the track and source into SQLite.
  4. Run full `analyze_audio` on the local file and save the key, BPM, RMS energy, and spectral brightness to `track_analysis`.

---

## 4. Running the Cleanup Utility

Run the 3-phase automated cleanup script using the Makefile target:
```bash
make db-cleanup
# or
PYTHONPATH=src .venv/bin/python scripts/db_cleanup.py
```
This script runs Phase 1 (Deduplication), Phase 2 (Auto-filling Sources), and Phase 3 (Orphan Cache Healing) in a single safe, atomic transaction.

---

## 5. TUI Scan Mode Fallback: Blank Browser Artist

Some browsers playing YouTube Music report an empty artist (`xesam:artist = ""`) and only provide the song title (e.g. `"Cup Runneth Over | YouTube Music"`, which is cleaned to `"Cup Runneth Over"`). 

When an exact `(artist, title)` lookup fails in `resolve_lyrics()`, implement a **title-only fallback**:
1. Query the `tracks` table by title: `SELECT t.track_id, t.artist, t.title FROM tracks t WHERE lower(t.title) = lower(?)`.
2. Prioritize matching rows that contain approved synced lyrics (`ORDER BY EXISTS(SELECT 1 FROM lyrics l WHERE l.track_id = t.track_id AND l.synced_lyrics != '') DESC, t.track_id DESC`).
3. Automatically attach the resolved artist and approved lyrics to the active playback timeline, showing them immediately in scan mode.

