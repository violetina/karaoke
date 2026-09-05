# Scan mode

**Auto-selected** when a desktop or browser player is playing something that
isn't Spotify — a YouTube / YT Music tab in Firefox or Chrome, VLC, or any other
MPRIS player with a real track. This is the everyday mode.

`detect.classify()` returns `scan` whenever a non-Spotify player reports a
title, artist or URL. Position comes from the player's own MPRIS `position`.

## What it does

1. Reads the active player's metadata and URL over MPRIS.
2. Resolves lyrics from the local cache, preferring a **URL match** against the
   `sources` table over the browser's artist/title (browser MPRIS metadata is
   frequently stale or truncated). See the
   [lyric-resolution flow](../flows.md#lyric-resolution-for-a-detection).
3. On a cache miss, fetches from LRCLIB in the background.
4. Highlights the active line/word against `elapsed = position - offset`.

## Auto-loading captions

When the tab is a YouTube / YT Music video with **no cached synced lyrics**, a
background worker auto-stages the video's captions. If they carry real timing
(json3 `synced`/`enhanced`) they're auto-approved into the cache and the
timeline reloads immediately — no manual `karaoke-stage youtube … && approve`.
Untimed captions are left in the staging queue for manual review. Each video is
attempted once per session and the fetch respects `KARAOKE_COOKIES_FROM_BROWSER`.

## Rescuing lyrics from the player's own panel

Captions only exist for some uploads, and they are a *subtitle track*, not the
lyrics tab. When LRCLIB has nothing and there are no usable captions, the
player itself may still be showing the words — YouTube Music's SONGTEKST tab,
attributed to LyricFind.

Those are read over the CDP connection the playback window already holds, stored
as plain text, and then queued for alignment: Whisper supplies a rhythm and the
real words are kept, so an unindexed track ends up properly timed. See the
[Lyric rescue flow](../flows.md#lyric-rescue-flow) for the schema and the
guards.

## Sync offset

Browser MPRIS `position` can run slightly **ahead** of audible output (device
buffering), so lyrics may lead the sound. The TUI subtracts a per-track offset
before highlighting:

- Nudge live with `,` (lyrics earlier, +0.1s) and `.` (lyrics later, −0.1s).
- `S` saves the current offset for the playing track (`track_sync_offsets`
  table); on a track change with an unsaved nudge, the TUI asks Save/Discard.
- Default is `0`; override with `KARAOKE_SYNC_OFFSET`.

See [Workflow → Lyric sync offset](../workflow.md#lyric-sync-offset-in-scan-mode)
for the full behaviour.

## Filling in metadata while scanning

Scan mode only reads the browser — it can't download audio itself. To get
key/BPM for what's playing, press `k` ([Sample mode](sample.md)) to record the
output and analyse it, or `O` ([Record mode](record.md)) to capture unattended.
`A` queues the track for post-processing (which needs a downloadable source).

## Related

- [Detection flow](../flows.md#detection-flow)
- [The TUI](../tui.md) · [TUI visuals](../tui-visuals.md)
