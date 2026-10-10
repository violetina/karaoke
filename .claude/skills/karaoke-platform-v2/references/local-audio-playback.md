# Local audio playback for Karaoke `--download`

Use this when `karaoke --youtube <url> --download` downloads/caches a file but then asks:

```text
Press Enter when the music starts...
```

## UX diagnosis

That prompt means the renderer has synced lyrics but no audio source it is controlling. In early implementations, `--download` only meant "make the audio available locally" for cache/transcription/beat detection; the lyric clock was still manually started by Enter.

For YouTube download mode this is misleading: once `resolve_youtube(..., download=True)` returns a `SongRef.path`, the app has a local audio file and should start it itself.

## Validated implementation pattern

- Keep the manual `Press Enter` prompt only for modes where there is no local audio path and the user is playing music elsewhere.
- When `SongRef.path` exists, pass it into the renderer.
- In the renderer, start local playback with:
  - `ffplay -nodisp -autoexit -loglevel error <path>` as first choice;
  - `vlc --intf dummy --play-and-exit <path>` as fallback.
- Start the lyric clock immediately after spawning the audio process.
- On Ctrl-C or renderer exit, terminate the spawned audio process if it is still running.

## Verification

Run automated tests, then test the interactive path with a short timeout so a long song does not block the agent:

```bash
pytest
timeout 8 karaoke --youtube "https://www.youtube.com/watch?v=<id>" --download
```

An exit code of 124 is expected if the timeout intentionally stops a long song. The important evidence is output like:

```text
Playing ~/.local/share/karaoke/youtube/<id>.webm
```

rather than blocking at:

```text
Press Enter when the music starts...
```
