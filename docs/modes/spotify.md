# Spotify mode

**Auto-selected** when the active MPRIS player's name starts with `spotify`.
`detect.classify()` maps it to `spotify` mode; the playhead comes from Spotify's
own reported position, which is accurate and needs no browser-lag correction.

## What it does

- Follows the currently-playing Spotify track and syncs lyrics to its position.
- Moves to the next track automatically as Spotify does.
- Lyrics are resolved from the cache first, then LRCLIB — Spotify mode does
  **not** download audio, so lyrics are cache/LRCLIB only.

## Sync offset

Spotify reports an accurate native position, so the default offset is `0` and it
usually needs no correction. Override with `KARAOKE_SYNC_OFFSET_SPOTIFY` if your
setup needs it; the same `,` / `.` / `S` nudge-and-save keys work.

## The key/BPM gap

A Spotify-only track has **no downloadable audio file**, so ordinary
post-processing has nothing to analyse and those tracks sit in the backlog
permanently. The fix is to record what's playing:

- `k` — [Sample mode](sample.md): record ~45s of the output and analyse it once,
  in real time. The result is stored with a `+sample` method suffix.
- `O` — [Record mode](record.md): capture unattended and decompile later.

Both read the PipeWire sink monitor, not Spotify, so they work regardless of how
Spotify exposes its audio.

## Note on launching

`detect.launch_spotify()` is deprecated — Spotify playback is unified into the
browser window rather than started as a separate desktop app.

## Related

- [Detection flow](../flows.md#detection-flow)
- [Sample flow](../flows.md#sample-flow) · [Sample mode](sample.md)
