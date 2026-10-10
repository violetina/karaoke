---
name: karaoke-plain-to-synced
description: Upgrade plain karaoke lyrics to synced via Whisper.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [karaoke, lyrics, whisper, transcription]
---

# Upgrade plain lyrics to synced lyrics

When plain-text lyrics exist for a track (e.g. from approving staged YouTube
captions via `karaoke-stage`) but synced (timestamped) lyrics are needed for
live players like `--player-follow`, this two-step workflow uses the Whisper
transcriber to generate them.

## When to use

- A track has plain lyrics but no synced lyrics, and you want to use it with a
  live/scrolling karaoke player.
- You have approved plain lyrics from `karaoke-stage` and now want to generate
  the LRC timestamps.

## Workflow

1.  **Download the audio**: Ensure a local audio file exists for the track. If
    the source was a YouTube video, the easiest way is with `karaoke-yt`:
    ```bash
    karaoke-yt 'https://www.youtube.com/watch?v=...' --download
    ```
    This will download the audio to the local cache (`~/.local/share/karaoke/youtube/`).
    Note the file path it creates (e.g., `.../youtube/<id>.webm`).

2.  **Force transcription**: Run `karaoke` on the downloaded audio file, using
    the `--force-transcribe` flag. This flag tells the app to ignore all
    existing cache entries (including the plain text version) and run Whisper
    to generate fresh, timestamped lyrics.
    ```bash
    karaoke -f ~/.local/share/karaoke/youtube/<id>.webm --force-transcribe
    ```
    This process will take a few moments as it analyzes the audio. When it
    completes, it automatically overwrites the local cache entry for that song
    with the new, synced version.

## Verification

After the transcription is complete, the live players will work automatically.
You can verify the result with `--player-follow` or by printing the lyrics,
which will now include timestamps:

```bash
karaoke --print "Artist - Title"
```
