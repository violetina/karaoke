# Recording REST API, Filtering, and Decompile Slicing Pipeline

This reference documents the REST API filtering surface for record mode, session update endpoints, and the internal arithmetic of how recorded audio is sliced, dated, and processed.

## 1. REST API Filtering & Options

### `GET /api/recordings` (Library API)
Read-only listing of recording sessions over SQLite with query options:
- `status`: Filter by status (`recording`, `complete`, `analysed`, `discarded`). Comma-separated lists allowed (e.g., `status=complete,analysed`).
- `source`: Substring filter on audio source name (e.g. `source=alsa` or `source=monitor`).
- `has_marks`: Boolean (`true`/`false`) filtering sessions with or without identification marks.
- `identified_only`: Boolean (`true`/`false`) filtering sessions with at least one identified track.
- `keep_audio`: Boolean (`true`/`false`) filter by `keep_audio` flag.
- `since` / `until`: Unix timestamp filters on `started_at`.
- `q`: Search query matching session `note` or `source`.
- `limit` / `offset`: Pagination (`limit=100`, `offset=0`).

### `GET /api/recordings/{recording_id}` (Library API)
Detailed session view with derived track list and options:
- `confident_only=true`: Filters returned tracks to those passing the confidence gate (`marks >= 2`, `spread <= 4.0s`).
- `min_marks=N`: Filters tracks with at least `N` identification marks.

### `PATCH /api/recordings/{recording_id}` (Library API)
Updates session metadata:
```json
{
  "note": "Session description or tag",
  "keep_audio": true
}
```

### `POST /api/record/start` (Control API)
Starts a PipeWire capture session on the desktop session:
```json
{
  "source": "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor",
  "keep_audio": false,
  "note": "Optional session note"
}
```

---

## 2. Recording Slicing & Dating Pipeline (`recording_slice.py`)

1. **Start Timestamp Arithmetic**:
   $$\text{start\_wall} = \text{at\_wall} - \text{at\_offset} - \text{RECOGNITION\_LEAD\_S}$$
   - `RECOGNITION_LEAD_S = 13.66s`: Measured constant correcting for Shazam sampling window + network recognition delay so track boundaries align with actual audio start times rather than arriving late.

2. **Mark Grouping & Run Isolation**:
   - Consecutive marks of the same `(artist, title)` within 240s (`MAX_MARK_GAP_S`) are grouped into a run.
   - Any failed mark (silence, speech, unknown track) or gap >240s terminates the run so separate plays are not merged.

3. **Median Start & Spread Gate**:
   - `start_wall`: Median of per-mark start estimates (immune to single outlier Shazam matches).
   - `spread`: `max(estimates) - min(estimates)`.
   - `is_confident`: `marks >= 2`, `spread <= 4.0s`, `duration > 0s`. Unconfident segments are gated out of automatic analysis.

---

## 3. Audio Extraction & Indexing (`recording_worker.py`)

1. **10-minute Segment Concatenation**:
   - Captured FLAC files (`seg-YYYYMMDD-HHMMSS.flac`) are located on the wall clock by timestamp in filename.
   - Overlapping FLAC files are concatenated and sliced via `ffmpeg -f concat -i concat.txt -ss <offset> -t <duration> -ac 2 -ar 44100 segment.wav`.
2. **Analysis & Storage**:
   - Analyzed for key/BPM via `librosa`/`essentia` (`analyze_audio`).
   - SQLite `track_analysis` updated with method `...+recording`.
   - 62-dimensional audio feature vector extracted and indexed into OpenSearch `karaoke-audio` with ID `audio:{track_id}:{start_wall}`.
