# Audio source selection and derived-feature cache

When recreating Spotify-style audio features locally, keep the metadata source and audio source separate.

## Durable lesson

Spotify is a good canonical metadata source (track id, title, artist, album, duration), but it is usually not an audio source: `preview_url` is commonly null and `/v1/audio-features` / `/v1/audio-analysis` can return 403 for standard developer apps.

YouTube or local files can provide analyzable audio, but they can mismatch the Spotify track: live versions, remixes, radio edits, sped-up/slowed uploads, karaoke/instrumental versions, and pitch-shifted copies are common.

## Recommended flow

1. Fetch canonical metadata from Spotify, if the task is about a currently playing Spotify track.
2. Check only a **derived-feature cache**; do not inspect or reuse Spotify's private playback cache.
3. If there is no trusted cache hit, make the audio source explicit:
   - user-provided local file,
   - user-provided YouTube URL,
   - ranked YouTube search candidates requiring confirmation,
   - Spotify preview only as a weaker fallback.
4. Run MIR locally on the selected audio source.
5. Cache the resulting derived features with enough source identity to avoid poisoning future lookups.

## Cache guidance

Do not key results by Spotify track id alone when the analyzed audio came from YouTube. Use a composite key that includes:

- analyzer version,
- Spotify track id or explicit metadata id,
- source kind (`youtube_url`, `youtube_search_candidate`, `local_file`, `spotify_preview`),
- source fingerprint (URL, file hash/size/mtime, or stable candidate URL),
- enough source metadata to inspect later (title, uploader, duration).

Suggested envelope:

```json
{
  "schema_version": 2,
  "entries": {
    "spotify:<track_id>|source:<fingerprint>|method:<analyzer_version>": {
      "metadata": {"provider": "spotify", "track_id": "...", "track_name": "...", "artists": ["..."], "duration_ms": 212000},
      "source": {"kind": "youtube_url", "url": "https://...", "title": "...", "duration_sec": 212, "fingerprint": "..."},
      "analysis": {"key": "F Minor", "key_confidence": 0.73, "tempo": 121.5, "energy": 0.084, "danceability": 0.517}
    }
  }
}
```

## Guardrails

Warn or require confirmation when:

- source duration differs from Spotify duration by more than ~10%,
- title/artist tokens do not overlap,
- YouTube title contains `live`, `sped up`, `slowed`, `karaoke`, `instrumental`, `cover`, or `remix`,
- result is based on a Spotify 30s preview rather than full-track audio.

## Spotify cache boundary

Do not build a workflow around Spotify's desktop/mobile local cache. It is private, DRM/cache-format dependent, platform-specific, and not a stable public interface. Cache only derived analysis results and non-secret source metadata.