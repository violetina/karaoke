---
name: karaoke-lyrics-platform
description: Build a local karaoke + lyric semantic-search platform.
version: 1.1.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [karaoke, lyrics, sqlite, lrclib, songrec, whisper, spotify, mpris, playerctl, embeddings, gentoo]
    related_skills: [song-identification]
---

# Karaoke + lyric semantic-search platform

Build a local platform that uses a self-contained SQLite database to cache
lyrics, manage a library, and drive a terminal karaoke app. The app can identify
songs via `songrec`, fall back to Whisper for transcription, sync to live desktop
players via MPRIS, and automatically backfill missing lyrics.

Reference implementation (working, verified live): **github.com/violetina/karaoke**.

## When to use

- User wants karaoke, "sing-along", or timed/synced lyrics in the terminal.
- User wants to automatically build a local, synced-lyric library based on music they listen to.
- Controlling desktop media players (`playerctl`) from the agent.
- Aligning existing plain-text lyrics to audio using Whisper.

## Architecture (SQLite-centric)

The application has been refactored to be a self-contained local utility, with
all core data (lyrics, history, backfill queue) stored in a single SQLite
database at `~/.local/share/karaoke/karaoke.db`.

The original OpenSearch/Kubernetes backend is no longer used for the main `karaoke`
command, making the application much lighter and dependency-free. The `lyricsearch`
(semantic search) command is temporarily unavailable but can be restored with a
lighter embedded vector search library in the future.

## Current Workflows

### Lyric Backfill Workflow (Manual)

This is the core process for adding new songs with synced lyrics.

1.  **Find Song URL:** Get the YouTube URL for the song.
2.  **Stage Captions (Optional):** Run `karaoke-stage youtube <URL>`. This may fail if no captions are available, or provide unusable junk like `[Music]`.
3.  **Review and Reject/Approve:** Use `karaoke-stage show <ID>` to review. If the lyrics are bad, run `karaoke-stage reject <ID>`. If good, `karaoke-stage approve <ID>`.
4.  **Find Plain Lyrics:** If captions are unusable, search the web (e.g., Genius, AZLyrics) for plain text lyrics and save them to a local file (e.g., `/tmp/lyrics.txt`).
5.  **Download Audio:** Use `karaoke-yt --download <URL>` to download the audio from YouTube. This saves the file to the local cache.
6.  **Generate Synced Lyrics:** Run `karaoke -f <path_to_audio> --force-transcribe --lyrics-file /tmp/lyrics.txt --print`. This uses Whisper to align the provided text with the audio, generating the final LRC-formatted lyrics and saving them to the database.

### Automated Lyric Backfill

The manual backfill workflow can be automated.

1.  **Gap Logging:** When `karaoke` is run with `--radio`, `--player`, or a query, and it fails to find any lyrics for a song, it now automatically adds an entry to a `lyric_gaps` table in the SQLite DB.
2.  **History Import:** A one-time script `scripts/import_history_gaps.py` can be run to populate the `lyric_gaps` table from all songs in the `play_events` history that are missing lyrics.
3.  **Backfill Command:** The `karaoke-backfill` command processes the `lyric_gaps` table.
    - `--list`: Shows all pending gaps.
    - `--run`: Iterates through pending gaps and automatically performs the manual backfill workflow (YouTube search, audio download, web search for lyrics, Whisper alignment).
4.  **Cron Job:** A cron job can be set up to run `karaoke-backfill --run` on a schedule (e.g., nightly) to continuously and automatically enrich the local lyric library.

### Player Control and Sync (URL-based)

Desktop player integration has been refactored to be URL-based, bypassing unreliable metadata from `playerctl`.

1.  **Get URL:** The `--player-sync` command now fetches the `xesam:url` from the current player.
2.  **Database Lookup:** It uses this URL to look up the track in the `sources` table of the SQLite database.
3.  **Fetch Lyrics & Sync:** If a match is found, it retrieves the canonical artist/title and synced lyrics from the `tracks` and `lyrics` tables and begins the sync loop. This ensures that even if `playerctl` reports incorrect artist/title metadata, the correct lyrics are displayed as long as the URL is known to the local database.

## Pitfalls (all hit during the real build)

- **MPRIS/playerctl unreliable metadata:** `playerctl metadata` for browsers like Firefox can return stale or completely incorrect artist/title information, even while providing the correct `xesam:url`. The `karaoke --player-sync` feature was rewritten to be URL-based to solve this. It now trusts only the URL, using it to look up the correct track in its own database, ignoring the artist/title from `playerctl`.
- **MPRIS/playerctl ambiguity:** When multiple players are active, a bare `playerctl play` can fail with "No player could handle this command". The `karaoke-player` script was updated with a `--player <name>` flag to target specific players like `firefox.instance_...`.
- **`--lyrics-file` for transcription:** The `--force-transcribe` feature was enhanced with a `--lyrics-file <path>` argument. This allows providing plain text lyrics from a file to Whisper, which then performs forced alignment to generate synced LRC lyrics, rather than doing its own speech-to-text. This is the core of the successful backfill workflow.
- **Not all tracks have synced lyrics** — LRCLIB may return plain-only; the player must degrade to printing plain lyrics, not crash.
- **YouTube Captions are often low quality:** The `karaoke-stage` workflow is essential for reviewing YouTube captions, which are often just `[Music]` or `[Applause]`. The `reject` command is as important as the `approve` command.
- **YouTube `--download` needs Deno (JS challenge), not just node** — As of 2025+, `yt-dlp` requires Deno on PATH to solve JavaScript challenges for audio stream URLs. Fix: `curl -fsSL https://deno.land/install.sh | sh` and add `~/.deno/bin` to PATH.
- **`yt-dlp` library vs. CLI:** For programmatic use, importing `yt_dlp` as a library is more robust than running `karaoke-yt` or `yt-dlp` as a subprocess and parsing stdout.
- **Whisper VAD can silently zero out a track** — `vad_filter=True` in faster-whisper can discard entire vocal tracks. It should be disabled by default for music.

## Key external facts (verified)

- **LRCLIB** (`https://lrclib.net/api/get?artist_name=&track_name=&album_name=&duration=`)
  returns `syncedLyrics` and `plainLyrics`, free with no key.
- **songrec** (from `media-sound/songrec` in the GURU overlay) does live song ID.
- **faster-whisper** with `word_timestamps=True` is the engine for aligning existing text to audio.
  - Set `condition_on_previous_text=False` to prevent looping on music beds.
  - The `small` model is a good balance of speed and accuracy for this task.
