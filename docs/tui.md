# The TUI (`karaoke-tui`)

A player-aware Textual shell. It watches the desktop over MPRIS, follows
whatever is playing, and renders time-synced lyrics.

## Layout

```
┌─ header ────────────────────────────────────────────────────────────┐
│  ___     _    _             ___                    ← figlet title   │
│ / __|___| |__| |___ _ _    | _ )_ _ _____ __ ___ _                  │
│ \___\___/_\__,_\___|_||_|  |___/_| \___/\_/\_/|_||_|                │
│   Gotye · scan · chromium · F major 129bpm · lrclib · offset +0.0s  │
├──────────────┬────────────────────────────────────┬─────────────────┤
│              │   Now and then, I think of when    │  mood square    │
│  cover art   │ ♪ Like when you said you felt so   │  key / bpm      │
│  (colour)    │   Told myself that you were right  │  sentiment arc  │
│              │   But felt so lonely in your comp  │  mood bars      │
│              │   ...                              │  rhythm         │
│              │                                    │                 │
│ source lrclib│                                    │                 │
│ lines  42    │                                    │                 │
│ length 4:05  │                                    │                 │
│ offset -0.3s │                                    │                 │
│ postproc done│                                    │                 │
│ workers  12  │                                    │                 │
│ cpu  [###__] │                                    │                 │
│ queue    0   │                                    │                 │
└──────────────┴────────────────────────────────────┴─────────────────┘
  Mode: scan (auto) · chromium          worker-load: [##___] 38% · queue 0
```

Both side columns are the same width, so the lyrics sit centred.

## Keys

Press `?` for the live list — it is generated from the bindings, so it cannot
go stale. The ones worth knowing:

| key | does |
|---|---|
| `H` | library overlay; picking a song closes it |
| `F` | focus mode — hides everything but the lyrics |
| `R` | mic/radio mode (songrec) |
| `A` | queue the playing track for post-processing |
| `T` | stats — library, pipeline, listening, keys, tempo |
| `?` | key reference |
| `,` `.` | nudge lyric sync ∓0.1s, `S` saves it for that track |

## Making the lyrics bigger

A terminal draws **one font at one size**, so the lyrics cannot be rendered
larger than the rest of the interface. Three approaches were tried and
rejected: figlet block type (unreadable at the sizes that fit), Unicode
fullwidth forms (drawn as tofu by most monospace fonts), and letter spacing
(breaks the reading rhythm).

What works instead: **raise the terminal font size**, and let the interface get
out of the way. A larger font means fewer cells, so the panels adapt:

| terminal | behaviour |
|---|---|
| < 110 columns | side columns hidden |
| < 32 rows | header compacted, title bar hidden |
| any size, `F` | everything hidden but the lyrics (93–96% of the screen) |

Measured lyric share at 80×24: **28% before, 77% after**.

## Stats (`T`)

Seven panels over the library and the pipeline:

```
library                     pipeline
  tracks       487            analysed     255  ######...... 52%
  karaoke-ready 422 ####. 87% backlog      232
  plain only     8            word timings   6
  no lyrics     57            workers    12 up
  sources      306            queue         0

lyric sources               keys              tempo
  lrclib       307 ####...     E minor  29      moderato 100-129  130
  whisper       72 #......     C minor  25      andante  70-99     54
  whisper_align 39 #......     A minor  20      allegro 130-159    44
```

`karaoke-ready` is the number that matters — tracks with *synced* lyrics, the
ones that can actually drive a session. `backlog` is what
`scripts/enqueue_postprocess.py` would pick up.

Degrades gracefully: a broker outage drops the worker rows and keeps the rest.

## Modes

- **scan** — a browser or desktop player is playing; position comes from MPRIS.
- **spotify** — Spotify is playing; sync and control it.
- **radio** (`R`) — songrec identifies room audio through the microphone. There
  is no MPRIS position, so the playhead is dead-reckoned from where songrec
  heard us plus elapsed time, plus a **12.6s forward lead**: songrec listens for
  ~10s before answering, so the offset it reports is already stale and the
  highlight would sit a couple of lines behind. `karaoke -r` applies the same
  figure (`DEFAULT_LEAD_S`, overridable there with `--lead`), so both modes stay
  in step. Declines to start if `karaoke --radio` already holds the mic.
- **browse** — nothing playing; drive the library list.

Lyrics are resolved from the cache first, then fetched from LRCLIB in the
background on a miss. Lookup tolerates spelling differences between sources —
songrec stores `James Brown & The Famous Flames` and `[2020 Remaster]` where a
browser reports plainer text — while still refusing to match a different
artist.

## Cover art

The left column shows the current track's artwork, drawn as coloured terminal
cells. It comes from the player's own `mpris:artUrl`, which browsers write to a
local file, so nothing is downloaded. Decoding goes through ffmpeg (already
required for downloads), which copes with PNG, JPEG or WebP alike.

Each pixel is a **space with a background colour** rather than a block
character: a space is unambiguously one cell wide, so the art cannot shift a
column on a terminal that draws block glyphs wide.

### Calibrating the aspect ratio

Terminal cells are roughly twice as tall as they are wide, but the exact figure
depends on the font and on any line spacing the terminal adds — and a TUI
cannot query it. The default is **2.5**, measured by eye on a real terminal
rather than taken from the theoretical 2.0. If the cover still looks stretched,
calibrate:

```bash
python -m karaoke.coverart          # prints a square
```

If that block looks taller than it is wide, raise the value; if squat, lower it:

```bash
export KARAOKE_CELL_ASPECT=2.4
```

## Environment

| variable | default | meaning |
|---|---|---|
| `KARAOKE_CELL_ASPECT` | `2.5` | cell height ÷ width, for cover art |
| `KARAOKE_SYNC_OFFSET` | `0.0` | seconds to pull lyrics back in scan mode |
| `KARAOKE_SYNC_OFFSET_SPOTIFY` | `0.0` | same, for Spotify |
| `KARAOKE_COOKIES_FROM_BROWSER` | — | browser to take YouTube cookies from |
