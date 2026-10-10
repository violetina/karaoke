---
name: synced-lyrics-and-karaoke
description: Fetch synced lyrics (LRCLIB) and sync them to playback.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [music, lyrics, lrclib, karaoke, spotify, whisper, opensearch, oauth]
---

# Synced lyrics + karaoke sync

Given an artist + title, fetch timestamped (LRC) lyrics and scroll them locked to
playback position. Complements the `song-identification` skill (songrec/whatsong
identifies WHAT is playing; this handles the lyrics + sync). All techniques here
were verified live building a local karaoke CLI.

> Note: `song-identification` (song ID via songrec/PipeWire) currently lives as a
> separate skill in the same `media/` category and overlaps this one's intro; a
> future consolidation could merge them under one music umbrella.

## When to use

- "Find the lyrics / show synced lyrics / build a karaoke thing."
- Any task needing timestamped lyrics for a known track.
- Syncing on-screen text to a track playing in Spotify (or a local file).

## LRCLIB — free synced lyrics, no API key

`GET https://lrclib.net/api/get?artist_name=<a>&track_name=<t>&album_name=<al>&duration=<sec>`
- 200 → JSON with `syncedLyrics` (LRC) and/or `plainLyrics`. `duration` is int
  seconds; album+duration sharpen the match but are optional.
- Miss → `GET /api/search?artist_name=&track_name=`, take first result with
  `syncedLyrics` (else first result).
- No key/auth. Be polite (~0.3s between calls), cache hard (personal use only;
  don't redistribute the cache).
- Reality: many tracks are PLAIN-ONLY (`syncedLyrics` empty) → cannot scroll.
  Don't promise synced karaoke for every track.

## LRC parsing (robust rules learned)

- One line can carry MULTIPLE `[mm:ss.xx]` stamps (repeated chorus) → expand each
  into its own (seconds, text) pair.
- Fractional part is 2 digits (centisec) OR 3 (ms): normalize
  `frac.ljust(3,'0')[:3]` then `/1000`.
- Drop metadata-only lines (`[ar:]`, `[ti:]`) and empty-text stamps; sort by time.
- active-line lookup = latest line with ts <= elapsed; return -1 for the intro.

## Live position-sync beats a keypress clock

Poll the player for position, interpolate between polls with a monotonic clock:
`anchor=(pos0, mono0)`; `elapsed = pos0 + (monotonic()-mono0) + offset`; re-poll
~1s to correct drift. Verified: RHCP "Under the Bridge" seeked to 60s, karaoke
line advanced in lockstep with the audio.

## Whisper fallback (LOCAL files only)

No LRCLIB synced lyrics + a local audio file → faster-whisper
`word_timestamps=True`, group words into lines by pause (>~0.8s) / max chars
(~42) / max words (~10); line time = first word start; render to LRC; cache once.
Approximate (base model mis-hears); bigger models + real audio do better.
Streaming services with no downloadable audio can't use this.

## Reusing Hermes' stored provider OAuth (key, broadly reusable)

See `references/lyrics-and-karaoke-sync.md` for the full recipe. Summary: Hermes
keeps provider OAuth in `~/.hermes/auth.json` under `providers.<name>`
(`client_id`, `refresh_token`, `granted_scope`, `access_token`, `expires_at`).
Standalone code can reuse it with NO separate login: read the refresh_token,
`POST accounts.spotify.com/api/token` with `grant_type=refresh_token` when
expired, call the API with the bearer token. Works for any provider Hermes has
authed. Read-only; never print token values.

## OpenSearch as a lyric + kNN vector store (if indexing)

- kNN is built in; `knn_vector` field dimension MUST match the embed model (384
  for all-MiniLM-L6-v2), `method hnsw / cosinesimil / lucene`, index `"knn": true`.
- OpenSearch 3.8 REMOVED `knn.plugin.enabled`; putting it in `opensearch.yml`
  crash-loops the node (`unknown setting [knn.plugin.enabled]`).
- Single-node: index `number_of_replicas: 0` or health stays yellow.
- Cache lookup by title must be EXACT (`match_phrase` on title AND artist + a
  case-insensitive guard). A fuzzy `match` with no score floor false-returned an
  unrelated cached track's lyrics.

## Reference

- `references/lyrics-and-karaoke-sync.md` — full LRCLIB/Whisper/OAuth-reuse/
  OpenSearch detail, code-level recipes, and the verified sync transcript.
