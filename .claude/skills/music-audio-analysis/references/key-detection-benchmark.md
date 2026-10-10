# Key Detection Benchmark

Measured results from validating a real key-detection implementation. Use these
tracks as the starting fixture for `scripts/benchmark_key_detection.py`.

## Vetted ground-truth tracks

| Track | Key | Sources |
|---|---|---|
| The Weeknd — Blinding Lights | F minor | tunebat, sfrbeats |
| Michael Jackson — Billie Jean | F♯ minor | musicnotes (sheet), sfrbeats |
| Adele — Rolling in the Deep | C minor | sheet music |
| Gotye — Somebody That I Used to Know | D minor | audiokeychain |

**Source disagreement is expected.** Tunebat lists *Rolling in the Deep* as
C♯ minor — it indexes a pitch-shifted (sped-up) upload. Sheet music says C minor.
Prefer notated/sheet sources when they conflict with BPM-database sites.

## Scoreboard — algorithms

Each track analyzed over 6 windows (intro, 35%, 70%, 3 seeded-random) plus full
track.

| Track | Naive (chroma argmax + flatness) | KS correlation | Bass-weighted KS | Essentia `edma` |
|---|---|---|---|---|
| Blinding Lights | F **Major** ✗ | F Minor ✓ | C Minor ✗ | **F Minor ✓** |
| Billie Jean | F♯ **Major** ✗ | F♯ Major ✗ | F♯ Minor ✓ | **F♯ Minor ✓** |
| Rolling in the Deep | G Major ✗ | G Minor ✗ | C Minor ✓ | **C Minor ✓** |
| Somebody That I Used to Know | D **Major** ✗ | B Minor ✗ | C Major ✗ | **D Minor ✓** |
| **Total** | **0/4** | 1/4 | 2/4 | **4/4** |

Hand-rolled librosa chroma + profile correlation topped out at 2/4. Tuning the
heuristics further was not the fix — swapping the library was.

## Scoreboard — Essentia profiles

| Track | edma | bgate | temperley | krumhansl | shaath |
|---|---|---|---|---|---|
| Blinding Lights | ✓ | ✓ | E♭ Major ✗ | C Minor ✗ | C Minor ✗ |
| Billie Jean | ✓ | ✓ | ✓ | ✓ | ✓ |
| Rolling in the Deep | ✓ | ✓ | E♭ Major ✗ | ✓ | ✓ |
| Somebody That I Used to Know | ✓ | ✓ | F Major ✗ | ✓ | ✓ |
| **Total** | **4/4** | 4/4 | 1/4 | 3/4 | 3/4 |

`edma` and `bgate` tie on accuracy; `edma` wins on confidence margins.
`temperley` fails badly on pop — it collapses minor keys into their relative
major (F minor → E♭ Major, D minor → F Major).

## Why multi-window voting is required

Final `edma` output per track:

```
Rolling In The Deep   -> C Minor   [conf 1.00, 6/6 windows]
Billie Jean           -> F# Minor  [conf 0.87, 5/6]
Somebody That I...    -> D Minor   [conf 0.54, 4/6]   <- flagged ambiguous
Blinding Lights       -> F Minor   [conf 0.61, 3/6]
```

On **Gotye** and **Blinding Lights**, Essentia's *full-track* read was wrong
(`C Major` and `C Minor` respectively). Only the window vote recovered the
correct key. Full-track analysis alone would have scored 2/4.

Gotye's `late 70%` window reads C major under every algorithm tested — that
section genuinely sits on the C–F–B♭ loop away from the D tonic. Adele's intro
reads G (the dominant). This is why a single 30s slice — especially the intro —
is unreliable.

## Failure analysis of the naive implementation

Two distinct bugs, both worth recognizing on sight in code review:

1. **Mode from spectral flatness.** `mode = "Minor" if flatness > 0.05 else "Major"`
   returned `"Major"` for all 24 windows across 4 songs, every one of which is in
   a minor key. Spectral flatness is a noisiness/tonality measure with no
   relationship to musical mode — the line was effectively a constant.

2. **Tonic from `argmax(sum(chroma))`.** Picks the most-frequent pitch class,
   which is routinely the 5th or the relative major's root rather than the tonic.
   Produced `F Major` for F minor and `D Major` for D minor — the pitch-class
   letter was often right while the mode and tonal center were wrong, which makes
   the output look plausible and hides the bug.

## Caveat on scope

4/4 on four well-known pop tracks is a smoke test, not a benchmark. Expect worse
on modal, jazz, key-changing, or pitch-shifted material. Report the confidence
and agreement numbers to the user rather than presenting the key as certain.
