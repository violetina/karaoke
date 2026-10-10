---
name: karaoke-platform-v2
description: Build a local karaoke + lyric semantic-search platform.
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [karaoke, lyrics, sqlite, lrclib, songrec, whisper, spotify, mpris, playerctl]
    related_skills: [song-identification]
---

# Karaoke + Lyric Platform with Automated Backfill

Build a local platform that provides a terminal-based karaoke experience. The system uses a self-contained SQLite database, fetches lyrics from LRCLIB, identifies songs via `songrec`, falls back to Whisper for transcription, and can sync to desktop media players.

A key feature is the automated "backfill" system. When you listen to a song via player or radio mode that has no lyrics, it's added to a "gaps" list. A cron job then automatically finds lyrics and audio for these missing songs, generating synced versions in the background.

Reference implementation (working, verified live): **github.com/violetina/karaoke**.

## When to use

- User wants a terminal-based karaoke or "sing-along" experience.
- User wants to create synced (LRC) lyrics for audio files.
- User wants to automate the process of finding and syncing lyrics for their music library.
- The task involves controlling desktop media players (`vlc`, `firefox`, etc.) from the command line.

## Architecture

1.  **Data Plane**: SQLite (`~/.local/share/karaoke/karaoke.db`) is the operational source of truth for tracks, sources, lyrics, stats, gaps, recordings and workflow state. OpenSearch is a derived index for semantic/vector search (`tracks`, `tracks-lines`, CLAP/audio vectors) and can be rebuilt from SQLite.
2.  **Lyric Acquisition**:
    - **Automated**: `karaoke-backfill` command (typically run via cron) processes missing songs.
    - **Manual**: `karaoke-stage` for adding lyrics from sources like YouTube captions, and `karaoke --lyrics-file` for using local text files.
3.  **Clients (Host CLI)**:
    - `karaoke`: The main app for display and playback.
    - `karaoke-player`: Controls MPRIS-compatible desktop players. 
    - `karaoke-stage`: Manages the review and approval of new lyrics.
    - `karaoke-backfill`: The automated process for filling lyric gaps by enqueueing Celery tasks.

## Key Workflows

### Automated Lyric Backfill (The "Self-Healing" Library)

This is the primary workflow for building the lyric library.

1.  **Gap Detection**: When using `karaoke --radio` or `karaoke --player`, if a song is identified but has no lyrics, it's automatically added to the `lyric_gaps` table in the SQLite database.
2.  **Processing**: The `karaoke-backfill --run` command (run manually or via a cron job) queries for pending gaps.
3.  **Execution (Celery Workflow)**: For each gap, a Celery task `karaoke.tasks.postprocess_track` is enqueued, which orchestrates the following sub-tasks:
    a.  **`resolve_track_id`**: Resolves the `track_id` and the most suitable YouTube URL.
    b.  **`download_audio`**: Downloads the audio file using `yt-dlp` if not already cached.
    c.  **`analyze_audio`**: Runs musical analysis (key, BPM, energy) and saves to `track_analysis`.
    d.  **`upgrade_timings`**: Upgrades line-level synced lyrics to Enhanced LRC using YouTube json3 captions if available.
    e.  **`sync_lyrics`**: Aligns plain lyrics to the downloaded audio using Whisper for word-level timings.
    f.  **`rebuild_vectors`**: Rebuilds audio and lyric vectors (e.g., for OpenSearch indexing).
    All sub-tasks are idempotent and retryable, and their progress/status can be monitored via the Flower dashboard.

### Manual Lyric Acquisition from Scratch

This workflow is used when automated processes fail or for new songs.

1.  **Identify Need**: A song is played, and the app reports "No synced lyrics."
2.  **Find Audio**: Find the song on YouTube.
3.  **Find Lyrics**: Find plain text lyrics from a web source like Genius or AZLyrics.
4.  **Save Lyrics**: Copy the plain text and save it to a local file (e.g., `/tmp/lyrics.txt`).
5.  **Download Audio**: Use `karaoke-yt --download <YOUTUBE_URL>` to download the audio.
6.  **Generate Sync**: Run `karaoke -f <path_to_audio> --force-transcribe --lyrics-file /tmp/lyrics.txt`. This aligns the text with the audio and saves the result to the cache. The next time you play the song, the synced lyrics will be available instantly.

## Pitfalls (all hit during the real build)

- **`playerctl` Metadata Unreliability**: The metadata reported by `playerctl` can be stale, incorrect, or ambiguous, especially with web browsers. A browser tab might report the metadata of a previously played video.
    - **Symptom**: `karaoke -p` shows the wrong song title.
    - **Workaround**: Use the `--player <PLAYER_NAME>` flag (e.g., `karaoke-player -p firefox.instance_1_1079 play`) for explicit control. When debugging, use `playerctl --list-all` to find the correct player instance name. For syncing, if metadata is wrong, fall back to using the `--artist` and `--title` override flags (a temporary feature).
- **YouTube Captions Can Be Unreliable**: As seen with the "Give It Away" test, YouTube's automatic captions can be low-quality or completely missing, even for popular songs. The staging/review workflow is critical to catch this. Always `karaoke-stage show <ID>` before approving.
- **`yt-dlp` Dependencies**: `yt-dlp` may require `deno` to be on the PATH to solve JavaScript challenges for downloading some audio streams.
- **Whisper Hallucinations**: When transcribing audio with no speech (like an instrumental), Whisper may return nothing or it may "hallucinate" words. The `--lyrics-file` feature is the correct way to handle these tracks.
- **Python Imports**: Be mindful of circular dependencies and private/public function access. Moving rendering logic (`_build_frame`) out of `player.py` broke several other modules that depended on it. The fix was to restore the shared rendering functions to `player.py`.

## Verification Recipe

- **Unit Tests**: `pytest` covers the pure logic (timing, text processing, database interactions).
- **CLI End-to-End Test**:
    1.  Pick a song with no lyrics in the cache.
    2.  Run `karaoke --print "Artist - Title"` to confirm no lyrics are found. This should create a gap entry.
    3.  Run `karaoke-backfill --list` to confirm the gap was logged.
    4.  Run `karaoke-backfill --run` and monitor the process.
    5.  Run `karaoke --print "Artist - Title"` again. This time, it should print the newly-generated synced LRC lyrics.
