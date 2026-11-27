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

## Keep Going from the radio (`collection.radio_keep_going_refill`)

With **🔁 Keep going** on and the live queue empty, every newly heard song
seeds the [one live queue](../flows.md#one-live-queue-dashboard-graph-ai-dj-tui)
with songs that sound like it. Radio mode (`karaoke -r` /
`karaoke-radio.service`) and the TUI mic mode (`R`) both trigger it.

- **5 at a time.** Each detected song resolves to its library track in
  Postgres (`find_track_id`, then `find_track_id_relaxed`; never an OpenSearch
  fuzzy hit) and adds 5 playable matches from `queue_suggest.suggest_for_track`
  (CLAP sound similarity, spectral fallback).
- **5 times, then rotate.** The queue grows 5 → 10 → 15 → 20 → 25. From the
  6th new song on, the oldest 5 are dropped and 5 new ones appended, so the
  queue stays at 25. Rotation only happens while nothing from the queue has
  been played (`current_index == 0`), so a followed queue is never cut under
  the follower.
- **No doubles.** Candidates are skipped when they match the heard song, any
  queued row, or a recently rotated-out row, by `track_id` and by normalised
  artist + title (duplicate library rows of the same song count as one).
- **Only on a change.** Hearing the same song again does nothing
  (`unchanged_song`).
- **Unknown or not-yet-analysed songs.** A song new to the library is queued
  for full processing on first hearing and has no vectors until its audio is
  downloaded and analysed. The refill retries every 15s for up to 10 minutes
  (`RADIO_KEEP_GOING_WAIT_S`) while radio still hears that song, and stops as
  soon as the song changes; the next detection then takes over. A miss never
  touches the queue.
- **Never overwrites your queue.** It only acts on an empty queue, a queue it
  started itself (`playlist_id = "radio-keep-going"`, shown as
  *Radio: Keep Going*), or a queue that has been played to the end. A playlist
  you loaded stays as it is. Keep going off means no radio refills.
- While radio owns the queue, the follower's tail refill
  (`collection.autopilot_refill`) stands aside (`radio_managed`), so the two
  never both add songs.

Each refill is logged (`radio keep-going: +5 tracks (rotated N, queue now M)`)
and recorded as a `radio_keep_going` queue event.

**If the queue stays empty**, check whether the heard song still has
`analysis`/`clap` pending (`postprocess_queue.needs_postprocessing`). If the
Celery chain finished but those steps are still pending, the audio download
failed. Look for `download failed` / `The page needs to be reloaded` in
`journalctl --user -u karaoke-celery-worker`. Upgrade yt-dlp, restart the Celery
units and re-enqueue the song with `full=True`
(see [Troubleshooting](../troubleshooting.md)).

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
- [One live queue](../flows.md#one-live-queue-dashboard-graph-ai-dj-tui)
- [Queue autopilot (Keep going)](../next-song-suggestion.md#queue-autopilot-keep-going-queue_suggestpy)
- [The TUI → Modes](../tui.md#modes)
