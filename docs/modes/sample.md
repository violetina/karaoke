# Sample mode

**Triggered with `k`** (`sample_key`) for the track in front of you — a one-shot,
real-time key/BPM detection. Also available as `karaoke-sample` / `make sample`.

Post-processing needs audio. For a YouTube-sourced track that's fine — the audio
can be downloaded. For anything played through **Spotify** it isn't: there's no
file, so key/BPM analysis has nothing to work on. The audio is right there in the
speakers, though, so sample mode records it.

See the full [Sample flow](../flows.md#sample-flow) for the schema. In brief:

## What it does

1. Records `DEFAULT_SECONDS` (45s, min 20s) of the PipeWire sink **monitor** —
   a clean digital copy of exactly what's playing, no microphone or room noise.
   The monitor is a different device from the mic, so this runs happily alongside
   [radio mode](radio.md).
2. Analyses the excerpt: Essentia (voted key) + librosa (tempo, energy,
   brightness), delegating to the isolated audio venv when this interpreter lacks
   the DSP stack.
3. Stores the result against the track (creating the track row if a Spotify-only
   track never had one), with a `+sample` method suffix.

In the TUI it runs in a worker thread (`_background_sample`) so the UI stays
responsive; it reports capture failures, and says plainly when the audio venv
isn't installed rather than reporting an unknown key.

## Reading the numbers

Capture is real time — 45s of audio takes 45s — which is why it's driven for one
track on demand rather than over a backlog. What comes back is an **excerpt**:

- **Key** is a global property and survives excerpting well.
- **Tempo** is reliable for steady material, less so where the track changes
  tempo.

The `+sample` suffix marks the result as excerpt-derived wherever it's shown, so
it's never mistaken for a full-track analysis.

## Which output gets recorded

`sample_audio.playing_sink()` picks the sink actually **carrying a stream**, not
the default sink — with a Bluetooth speaker paired alongside built-in speakers,
the default is regularly not where the music is routed. Check what would be
recorded:

```bash
karaoke-sample --list-sinks
```

## CLI

```bash
make sample SECS=45 ARTIST="…" TITLE="…"     # record + analyse + store
karaoke-sample -t 45 --artist "…" --title "…" [--keep]
```

Without `--artist`/`--title` it prints the key/BPM but stores nothing.

## Sample vs Record

Sample analyses **one track now, in real time**. [Record mode](record.md)
captures **unattended** and decompiles a whole session offline. Both read the
sink monitor and both mark their results with a method suffix (`+sample` vs
`+recording`).

## Related

- [Sample flow](../flows.md#sample-flow) · [Record mode](record.md)
- [Local audio analysis](../workflow.md) (Dataflow & mitigation utilities)
