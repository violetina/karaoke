# Database Cleanup, Deduplication, and Self-Healing Workflow

## 1. Root Cause of Track Gaps & Duplicates

Because player metadata from Spotify or browsers (MPRIS) varies from lyric provider metadata (LRCLIB, Genius) and YouTube upload formats (uploader vs Topic channels), duplicate and incomplete track records can arise in SQLite:
* **Duplicate Track Records:** Spotify lists the artist as `"Inpatient"`, whereas LRCLIB lists the artist as `"Inpatient, Ren & Chris Webby"`. This results in two disjoint track rows. When playing in Spotify mode, the TUI looks up `"Inpatient"` and finds no lyrics, while in Browse mode the track `"Inpatient, Ren & Chris Webby"` is visible with full lyrics.
* **Orphan Lyrics (Missing Sources):** A track has approved lyrics in the `lyrics` table but has no matching row in `sources`, preventing the TUI from opening the track when Enter is pressed.
* **Orphan Cache Files:** YouTube downloads (`*.webm`) exist in the local cache but are missing corresponding database tracks or sources in SQLite.

## 2. Mitigation and Self-Healing System

To heal and maintain the SQLite database, use the automated cleanup utility `make db-cleanup` (`scripts/db_cleanup.py`). It executes a 3-phase self-healing loop:

### Phase 1: Track Deduplication and Merging
* Automatically groups all tracks by lowercase, trimmed title.
* Compares artist names case-insensitively using fuzzy compatible matching:
  - Substring checks (e.g. `"Inpatient"` matches `"Inpatient, Ren & Chris Webby"`).
  - First-word matching (e.g. `"Inpatient"` matches `"Inpatient (Ren x Chris Webby)"`).
  - Suffix cleaning (strips `- Topic`, `(Topic)`, `Official`, and punctuation).
* Consolidates all associated lyrics, sources, and track analyses into the canonical record, keeping approved synced lyrics over plain text and retaining complete analysis data.
* Deletes the duplicate record from the `tracks` table.

### Phase 2: Source Webpage Healing
* Finds any track that contains approved lyrics but lacks an entry in the `sources` table.
* Automatically performs a YouTube search for `"{artist} {title}"`, retrieves the webpage URL, and inserts a source row (`kind='youtube'`).

### Phase 3: Orphan Cache Healing
* Scans `~/.local/share/karaoke/youtube/` for `*.webm` files whose video IDs are missing in SQLite `sources`.
* Fetches metadata from YouTube, cleans the parsed title/uploader, creates missing track/source records in SQLite, and runs full audio analysis (Key, BPM, energy, brightness) on the local file.

## 3. Title-Only Fallback for YT Music Scan Mode

When playing music in browser tabs like YouTube Music, playerctl may report an empty artist string (`xesam:artist = ""`) and only a title (e.g. `"Cup Runneth Over | YouTube Music"`, which normalizes to `"Cup Runneth Over"`). 

To prevent search misses, `resolve_lyrics` employs an automatic fallback:
* If the fuzzy `(artist, title)` lookup fails, and the artist is empty, it queries the `tracks` table by `title` only.
* It prioritizes matching tracks that have approved synced lyrics in SQLite.
