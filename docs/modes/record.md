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

Offline, at full speed — an evening analyses in minutes:

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

## HTTP

Record-mode sessions are also exposed over the API: `GET /api/recordings` and
`GET /api/recordings/{id}`.

## Related

- [Record flow](../flows.md#record-flow) · [Sample mode](sample.md) · [Radio mode](radio.md)
