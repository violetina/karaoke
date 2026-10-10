# Folder Scan Engine & Audio Detector Lessons

## 1. Extension Support for Local Audio
- Audio tags via `mutagen` (`tags.py`) check `AUDIO_EXTS`. YouTube cached downloads use `.webm`, so `.webm` must be included in `AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".webm"}`. Without `.webm`, the scanner skips YouTube cache files entirely.

## 2. Multi-tier Metadata Resolution
When scanning local directories (`karaoke.folder_scan` / `karaoke-folder-scan`), files frequently lack ID3 tags or carry generic placeholders (`artist="Unknown"`, `title="track01"`). `folder_scan` uses a 5-tier resolution pipeline:

1. **Tag Extraction:** mutagen `tags.extract_tags(path)`.
2. **YouTube Video ID Fallback:** Extract 11-character YouTube video ID from path/filename (`localcache.extract_youtube_id(path.name) or extract_youtube_id(str(path))`). If matched, query SQLite `sources` table (`WHERE s.url LIKE '%vid%'`) to retrieve canonical `(artist, title)` instantly without network requests.
3. **Recording Session Fallback:** For ambient captures in `recordings/` (`seg-*.flac`), match parent folder name against `recordings.dir` in SQLite, then read `recording_marks` via `recording_slice.segments(marks)` to resolve the identified track title.
4. **Shazam / Fingerprint Fallback:** `identify.identify_file_fingerprint(path)` cuts a 10s ffmpeg WAV slice and runs `songrec recognize -j` to query Shazam servers for untagged local files.
5. **Filename-based Fallback Parser:** If tags, recording session, and fingerprint are all missing or unknown, split the file stem on common separators like ` - ` or `-`. Clean track-number prefixes (e.g. `07 `, `(01) `, `01. `) from the artist name, and map them to `artist` and `title` fallback values (e.g. `07 Gadjo - Besame Mucho.mp3` -> `artist="Gadjo"`, `title="Besame Mucho"`).

## 3. Fast Streaming Resolution
- When scanning files whose YouTube ID is already known from path or DB lookup, skip `youtube.search()` and construct `https://www.youtube.com/watch?v={vid}` directly. This turns folder scanning on cached audio from seconds-per-file down to milliseconds.
