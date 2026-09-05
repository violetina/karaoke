# Radio mode

**Turned on with `R`** (`toggle_mic`). There is no MPRIS player to follow — the
microphone listens to the room and songrec (Shazam) identifies what's playing.
`Detection.is_active` treats `radio` as active because there is a song to sync
lyrics to, even without a player to control.

## What it does

- songrec listens through the microphone (~10s), returns the track and an
  offset into it, and lyrics are resolved and synced from there.
- Re-anchors the same track on repeated matches and swaps the timeline when a new
  track is identified.
- Keeps rendering through speech, ads and quiet sections that don't match.
- Declines to start if `karaoke --radio` already holds the mic.

## The forward lead

There is no live position source, so the playhead is **dead-reckoned**: where
songrec heard us, plus elapsed time — plus a **12.6s forward lead**. songrec
listens for ~10s before answering, so the offset it reports is already stale and
the highlight would sit a couple of lines behind. `karaoke -r` applies the same
figure (`DEFAULT_LEAD_S`, overridable there with `--lead`), so the TUI and CLI
stay in step.

## Matching tolerance

Lookup tolerates spelling differences between sources — songrec stores
`James Brown & The Famous Flames` and `[2020 Remaster]` where a browser reports
plainer text — while still refusing to match a different artist. This is the
`find_track_id_relaxed` step in the
[lyric-resolution flow](../flows.md#lyric-resolution-for-a-detection).

## Radio vs Record

Both listen to the room, but:

- **Radio** reads the **microphone** and follows one track live, for singing along.
- **Record** ([record.md](record.md)) reads the **sink monitor** (a different
  device) and captures unattended for later analysis.

Because they use different devices, radio and record compose — you can record
while radio mode is following along.

## Related

- [Detection flow](../flows.md#detection-flow)
- [The TUI → Modes](../tui.md#modes)
