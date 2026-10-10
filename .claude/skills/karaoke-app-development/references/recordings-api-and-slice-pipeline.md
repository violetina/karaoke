# Recordings REST API & Decompile Pipeline

This reference documents the REST API options for record-mode sessions and the 3-step pipeline that matches song recordings to tracks, slices continuous audio, and computes audio features.

---

## 1. REST API Surface for Recordings

### Library API (`karaoke.api`)

- `GET /api/recordings` — List record-mode sessions (newest first). Supports filtering, search, and pagination:
  - `status`: filter by status (e.g. `complete`, `analysed`, `discarded`, or comma-separated `complete,analysed`)
  - `source`: substring search on audio source (e.g. `pactl`, `monitor`, `alsa`)
  - `has_marks`: `true` (at least 1 mark) or `false` (no marks)
  - `identified_only`: `true` (at least 1 identified mark) or `false` (0 identified)
  - `keep_audio`: `true` or `false`
  - `since` / `until`: Unix timestamp bounds on `started_at`
  - `q`: search substring matching `note` or `source`
  - `limit` / `offset`: pagination parameters (default `limit=100`, `offset=0`)

- `GET /api/recordings/{recording_id}` — Inspect one session with derived track list:
  - `confident_only`: `true` to filter output to confident track identifications only
  - `min_marks`: minimum mark count threshold per track

- `PATCH /api/recordings/{recording_id}` — Update session metadata:
  - Body: `{"note": "Optional note", "keep_audio": true}`

### Control API (`karaoke.ctrl_api`)

- `POST /api/record/start` — Begin a PipeWire capture session:
  - Body: `{"source": "optional.monitor", "keep_audio": false, "note": "optional description"}`
- `POST /api/record/stop` — Stop capture session(s):
  - Body: `{"recording_id": 12}` (or omit `recording_id` to stop all)
- `POST /api/recordings/{recording_id}/analyse` — Trigger background decompile into SQLite + OpenSearch vector index.
- `DELETE /api/recordings/{recording_id}/audio` — Discard raw audio files while keeping session markers.

---

## 2. Recording to Track Slicing & Processing Pipeline

### Step 1: Identification & Dating (`karaoke.recording_slice`)

1. **Identification Marks (`Mark`)**: While audio is continuously captured into 10-minute FLAC segments (`seg-YYYYMMDD-HHMMSS.flac`), a background thread queries Shazam (`songrec`) every ~45s.
2. **Start Estimate (`start_estimate`)**:
   $$\text{start\_wall} = \text{at\_wall} - \text{at\_offset} - \text{RECOGNITION\_LEAD\_S}\ (13.66\text{s})$$
   The 13.66s correction (`RECOGNITION_LEAD_S`) offsets the Shazam sample window + network recognition delay, accurately aligning boundary timestamps.
3. **Mark Grouping & Median Estimate**: Consecutive marks for the same `(artist, title)` within 240s (`MAX_MARK_GAP_S`) are grouped. The segment start time uses the **median** of all start estimates in the group to eliminate Shazam outlier noise.
4. **Confidence Gate (`is_confident`)**: Requires `marks >= 2`, `spread <= 4.0s`, and `duration > 0s`. Unconfident segments are gated out of automatic analysis.

### Step 2: Audio Slicing (`karaoke.recording_worker`)

1. **FLAC Placement (`segment_files`)**: Reads segment open timestamps directly from FLAC filenames.
2. **Span Clamping (`clamp`)**: Trims segment bounds to captured audio range `[min_start, max_end]`. Requires $\ge 20.0\text{s}$ remaining captured audio.
3. **FFmpeg Slicing (`cut`)**: Generates a temporary `concat.txt` for overlapping FLAC files and executes `ffmpeg -f concat -i concat.txt -ss <offset> -t <duration> -ac 2 -ar 44100 segment.wav`.

### Step 3: Analysis & Vector Indexing

1. **Audio Analysis (`analyze_audio`)**: Extracts Musical Key, Key Confidence, BPM, Energy (RMS), and Brightness via `librosa`/`essentia`.
2. **SQLite Storage**: Finds or creates the track in SQLite and saves analysis into `track_analysis` with method method tagged as `...+recording` (e.g. `essentia+recording`).
3. **Spectral Vector Indexing (`_index_audio_vector`)**: Computes a 62-d spectral feature vector (20 MFCC mean + 20 MFCC std + 12 chroma + 7 spectral contrast + 3 scalars) and indexes into OpenSearch `karaoke-audio` (`audio:{track_id}:{start_wall}`).
