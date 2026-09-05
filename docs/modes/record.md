# Record mode

**Turned on with `O`** (`toggle_record`). The unattended counterpart to
[Sample mode](sample.md): leave it running for an evening and it captures
everything coming out of the speakers while, in parallel, asking songrec every
so often what is playing. Each answer is stored as a **marker**, so the session
can be cut back into tracks and analysed offline afterwards.

The recording is a means to metadata, not a library — the audio is discarded
once analysed unless `keep_audio` is set.

See the full [Record flow](../flows.md#record-flow) for the schema. In brief:

## Capture

`recorder.start()` spawns a segmenting ffmpeg on the PipeWire sink **monitor**
(a clean digital copy of the output, and a different device from the mic, so it
composes with [radio mode](radio.md)). Audio is written as 10-minute FLAC
segments named for the wall-clock instant they open (`seg-YYYYMMDD-HHMMSS.flac`),
so the timeline survives a crash. An identification thread asks songrec every
~45s and writes a marker (`at_wall`, `at_offset`, artist, title, ok) each time —
successes **and** failures, because a gap is real evidence about the recording.

Caps keep a forgotten session from filling the disk: **8 hours** or **6 GB**,
and identification backs off ×4 after 3 consecutive misses (a podcast or silence
shouldn't hammer Shazam all night). Stopping SIGTERMs ffmpeg so it finalises the
segment it's writing rather than losing the tail.

The sidebar shows a live recording indicator (id, elapsed, identified/total
marks, size, source, blinking dot).

## Decompile

Offline, at full speed — an evening analyses in minutes.

**Stopping with `O` starts this automatically.** Recording and analysing used to
be separate steps with nothing joining them, so finished sessions just
accumulated: four of them, 987 MB, before anyone noticed. A session that
identified nothing is skipped, since with no markers there is no track list to
cut. Analysis deliberately does *not* run on app exit — starting minutes of work
during shutdown would be worse than leaving it — so a session closed by quitting
mid-recording is caught by the reminder at mount instead.

To drive it by hand, or to pick up a session closed that way:

```bash
make recordings                 # list sessions
make recording-show ID=<id>     # derived track list, nothing analysed
make recording-analyse ID=<id>  # cut, analyse, store (needs the audio venv)
```

Or `karaoke-recording --list / --show / --analyse / --discard`. Markers are
turned into tracks by `recording_slice`: a marker *dates* a track
(`start_wall = at_wall - at_offset`), and agreement between a track's markers is
what makes a boundary trustworthy. Segments with too few marks or too wide a
spread are reported for manual review but **gated out** of automatic analysis —
a key stored against the wrong track is worse than none.

Stored analyses carry a `+recording` method suffix so they're never mistaken for
one done on a downloaded master.

## Stale sessions

A row still marked `recording` with nothing running is a crash, not a live
capture. `recorder.reconcile_stale()` closes those out (the TUI runs it on
mount) so the listing doesn't lie and the audio becomes analysable.

The TUI also stops any capture it owns on exit — without that the ffmpeg child
outlived the app, its row stayed `recording` forever, and the next TUI started a
*second* recorder on the same source. One ran unparented for 1h51m alongside a
live one before this was fixed.

At mount it reports how many finished sessions are still unanalysed. Otherwise
they are invisible: the audio sits on disk and the tracks never gain a key.

## HTTP

Inspecting a session is read-only over SQLite, so it lives in the
cluster-deployable API:

```
GET /api/recordings          sessions with marks, size, status
GET /api/recordings/{id}     one session and the track list it resolves to
```

Driving one needs PipeWire, a desktop audio session and a long-lived ffmpeg
child, none of which exist in a container, so that lives in the host-side
control API:

```
POST   /api/record/start
POST   /api/record/stop
GET    /api/record/status
POST   /api/recordings/{id}/analyse     returns immediately; poll the status
DELETE /api/recordings/{id}/audio       drop the audio, keep the markers
```

Two distinctions the responses keep separate on purpose: `status` is what the
database says while `running` is whether a capture is live *in the answering
process* (a row can read `recording` after a crash, and sessions live only in
the process that started them); and a failed capture is 409 while a missing
audio stack is 503, because collapsing them would hide which one to fix.

## Related

- [Record flow](../flows.md#record-flow) · [Sample mode](sample.md) · [Radio mode](radio.md)
