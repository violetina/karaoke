---
name: music-audio-analysis
description: "Detect musical key/tempo from audio; benchmark first."
version: 1.0.0
metadata:
  hermes:
    tags: [Audio, Music, Key Detection, Tempo, Essentia, librosa, MIR]
---

# Music & Audio Analysis (MIR)

Extracting musical attributes — key, mode, tempo, energy — from audio files, and
validating that the extraction is actually correct before shipping it.

Use when: detecting song key/BPM, analyzing a Spotify/YouTube track locally,
reviewing or fixing existing audio-analysis code, or building anything that
claims to identify musical attributes.

Related: `songsee` (spectrogram/feature *visualization*; keep that for images,
use this skill for numeric key/tempo extraction and validation).

## Core rule: benchmark before you claim accuracy

Never ship or endorse a key-detection implementation without running it against
tracks with documented ground truth. This is cheap and non-negotiable — it is
how a 0/4 implementation gets caught instead of silently shipping.

Do NOT quote published accuracy figures ("~85-90% on pop") as if they were
measured. If you did not measure it, say so explicitly. A 4/4 on four songs is
encouraging, not a benchmark — state that limitation to the user.

Ready-to-run harness: `scripts/benchmark_key_detection.py` (see below).

## Key detection: what works

Use **Essentia `KeyExtractor` with `profileType="edma"`**, voted across multiple
windows.

```python
import essentia, essentia.standard as es
essentia.log.infoActive = False      # silences noisy [INFO] lines
essentia.log.warningActive = False

audio = es.MonoLoader(filename=path, sampleRate=44100)()
key, scale, strength = es.KeyExtractor(profileType='edma', sampleRate=44100)(audio)
```

Profile choice matters a lot. Measured on a 4-track pop benchmark:
`edma` 4/4, `bgate` 4/4, `krumhansl` 3/4, `shaath` 3/4, `temperley` 1/4.
Prefer `edma` (highest confidence margins); `temperley` is notably bad on pop.

### Multi-window voting is load-bearing, not decoration

Analyze several windows — intro, ~35%, ~70%, plus seeded-random slices — and
vote by summed `strength`. Weight the full track heavier (~1.5x) but not enough
to override a consistent window majority.

This is not belt-and-braces. On two of four benchmark tracks the *full-track*
Essentia read was WRONG and only the window vote recovered the correct key.
A single slice is a coin flip: intros are harmonically incomplete, and bridges
routinely sit on the relative major or the dominant.

Use a seeded RNG (`random.Random(42)`) so results are reproducible.

### Report confidence honestly

Emit the winning key, its confidence share, window agreement (`4/6`), the
full-track result, and runner-up keys. Flag `confidence < 0.5` as ambiguous
rather than presenting a coin-flip as fact.

## Anti-patterns (these are real bugs, not just imprecision)

- **Never derive Major/Minor from spectral flatness.** Flatness measures
  noisiness; it has no relationship to mode. A real-world implementation using
  `flatness > 0.05` returned "Major" for all 24 windows of 4 minor-key songs —
  it was effectively a constant.
- **Never use `argmax` of summed chroma as the tonic.** It picks the most-played
  pitch class, which is routinely the 5th or the relative major's root. This
  yields `F Major` for F minor, `D Major` for D minor.
- **Don't prefer a 30s preview over the full track.** Spotify's `preview_url` is
  null for most tracks now; fetch full audio via `yt-dlp` and fall back to the
  preview only if that fails.
- **Version your cache.** Store an `ANALYZER_VERSION` alongside results so a
  cache full of wrong answers is recomputed, not served forever.

## Ground truth: cross-check the web, expect disagreement

Verify against several sources (tunebat, sfrbeats, musicnotes sheet music,
audiokeychain). They genuinely disagree, usually for structural reasons:
- **Pitch-shifted uploads** — tunebat indexes a sped-up upload of *Rolling in
  the Deep* as C♯ minor; sheet music says C minor.
- **Enharmonic/relative confusion** in the sources themselves.
Prefer sheet-music sources for the notated key. See
`references/key-detection-benchmark.md` for the vetted track list.

## Dependency isolation (heavy DSP stacks)

`essentia` (large native binary) + `librosa` (numba/llvmlite/scikit-learn/scipy)
are heavy. Do not add them to a project's main requirements just to power a
side-tool.

**Mirror the repo's existing precedent rather than inventing a layout.** Check
for an established pattern first (`ls requirements*.txt`, grep the Makefile for
`VENV`). If the repo already isolates one stack, make yours a sibling of it:

```
requirements-audio.txt     ->  .venv-audio   ->  make install-audio
requirements-docs.txt      ->  .venv-docs    ->  make install-docs   (existing)
```

Add the new venv to `.gitignore`, and have the analyzer degrade gracefully —
guard the import and return key `"unknown"` rather than silently falling back to
an unreliable detector.

Version notes: essentia only publishes a dev/beta wheel for Python 3.14
(`essentia==2.1b6.dev1438` works, pin it exactly). `ffmpeg` is a system dep, not
pip-installable. Pyright will flag the guarded essentia import as unresolved —
expected inside `try/except ImportError`.

### Pitfall: essentia MonoLoader can't decode webm/opus (segfault or "Unsupported codec!")

essentia's bundled `AudioLoader`/`MonoLoader` rejects webm/opus (common for
yt-dlp downloads) with `RuntimeError: ... Unsupported codec!`, and mixing that
failure with a librosa call in the same process can SIGSEGV. Fix: transcode
unsupported containers to a temp mono WAV with system ffmpeg FIRST, then point
both essentia and librosa at the WAV. Gate on extension (`.wav/.flac/.ogg/.mp3`
load natively; everything else -> ffmpeg `-ac 1 -ar 44100 -vn`). Transcode once
and reuse the WAV for both key and tempo.

### Pitfall: full-track weighting can override the window majority

The "weight full track ~1.5x" advice is a trap if you add the weighted
full-track vote into the SAME tally as the windows: on split tracks (e.g. Gotye
"Somebody" — 3 D-minor vs 3 C-major windows) the wrong full-track read (C major)
tips the sum and loses the correct D minor. Rule that scored 4/4: windows are
authoritative — the windowed-strength leader wins outright; use the full-track
read ONLY as reporting metadata and as the fallback when there are zero usable
windows. Gotye then lands D minor at conf 0.50 / 3-6 (correctly flagged
ambiguous).

## Support files

- `scripts/benchmark_key_detection.py` — run before/after any algorithm change;
  takes a JSON fixture, prints per-window results and an accuracy scoreboard.
- `references/key-detection-benchmark.md` — vetted ground-truth tracks, measured
  profile comparison, and the full failure analysis of the naive approach.
