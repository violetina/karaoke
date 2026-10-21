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

## Song-change scan planning (`karaoke.songwatch`)

Rather than running songrec on an arbitrary fixed 30-second loop (~86 queries/hour), radio mode schedules scans based on track duration:

- **After a match**: Plans the next scan **1 minute before the expected end** (catching radio edits and DJ cuts, which typically truncate 30–60s) and a follow-up scan right as the song ends. Intervals are bounded between 20 seconds and 5 minutes (or 60 seconds if track duration is unknown).
- **After a miss** (talk, DJ banter, station jingles): Backs off exponentially through 30s → 60s → 120s → 180s.
- **Audio level end detection (`EndDetector`)**: Streams audio via `parec` and triggers an early scan when audio level drops 30 dB below the track's loudness (18 dB on room microphones) for 1.5 seconds. This detects track fade-outs and track transitions instantly.
- **Reduction**: Cuts query frequency by ~58% (from ~86 scans/hour down to ~36/hour) with zero mid-song false triggers.
- **Restoring fixed scans**: `--fixed-scan` restores the legacy fixed 30s clock if desired. The CLI status display shows when the next scan is scheduled and why.

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
