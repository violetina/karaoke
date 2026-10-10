---
name: linux-audio-pipewire
description: "Select/meter mics, capture audio, ID songs on PipeWire."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [pipewire, pulseaudio, pactl, wpctl, parec, audio, microphone, vu-meter, songrec, shazam, gentoo]
---

# Linux Audio Tooling (PipeWire)

Build and drive command-line audio tools on a PipeWire desktop: pick/meter the
microphone, capture output audio, and identify a playing song. Uses the
PulseAudio-compat layer (`pactl`, `parec`) plus WirePlumber (`wpctl`) — these are
present on essentially every PipeWire box and are more scriptable than the GUI.

## When to use

- "set / test / meter my mic", "which mic is default", "mic is too quiet/loud/clipping"
- "capture what's playing", "record desktop audio"
- "what song is this / playing" (song fingerprinting)
- Preparing audio input for Hermes voice mode (ctrl+b records from the *default* source)

## Core PipeWire facts (get these right or scripts silently fail)

- **`pactl list short sources` fields are TAB-separated**, and the sample-spec
  column contains spaces (`s16le 2ch 48000Hz`). Splitting on whitespace shifts
  every later column. Always `while IFS=$'\t' read -r idx name driver spec state`.
- **Sources include `.monitor` loopbacks** (the output sink's monitor) alongside
  real mics. Filter `grep -v '\.monitor'` when listing *microphones*; use the
  monitor when you want to capture *what's playing*.
- **Multi-sink machines**: `pactl get-default-sink` is often NOT the sink audio is
  routed to. To capture what's actually playing, resolve the sink from the active
  stream: `sink-inputs` row → its sink index → `sinks` name → `${name}.monitor`.
  Fall back to the default sink only when nothing is playing.
- **Control via stable names, not indices.** Indices renumber across
  suspend/replug; `alsa_input.*` names are stable. `pactl set-default-source NAME`,
  `set-source-volume NAME 70%`, `set-source-mute NAME toggle`.
- **`wpctl status`** gives a human tree (Devices/Sinks/Sources with `*` default and
  `[vol: x]`); good for a quick read. `wpctl` uses numeric node IDs; `pactl` uses
  names. Prefer `pactl` for scripting stable behavior.

## VU meter / mic test pattern (no extra deps)

`parec` raw PCM piped into a tiny Python RMS/peak reader — works with only
`parec` + system python, no `sounddevice`/`numpy`:

```bash
parec -d "$SRC" --format=s16le --channels=1 --rate=16000 --raw \
  | python3 -u -c 'import sys,struct,math
CH=1600; W=40; buf=b""
while True:
    d=sys.stdin.buffer.read(CH*2-len(buf))
    if not d: break
    buf+=d
    if len(buf)<CH*2: continue
    s=struct.unpack("<%dh"%CH, buf[:CH*2]); buf=buf[CH*2:]
    pk=max(abs(x) for x in s)/32768; rms=math.sqrt(sum(x*x for x in s)/len(s))
    db=20*math.log10(rms/32768) if rms>0 else -99
    print("\r[%s] %5.1f dB peak=%3d%%\033[K"%("#"*int(pk*W)+"-"*(W-int(pk*W)),db,int(pk*100)),end="",flush=True)'
```

Bound it with `timeout Ns parec ...`; **`timeout` exits 124 on normal elapse** —
treat 124 as success (`[[ $rc -eq 124 || $rc -eq 0 ]]`).

Meter pitfalls learned the hard way:
- **Redraw MUST clear to end-of-line (`\033[K`)** or a longer line's tail (e.g. a
  `CLIP!` flag) lingers next to a later shorter/quieter line — looks like
  "peak=6% CLIP!" which is nonsense. The clip *counter* was right; only the draw
  was stale.
- **A single clipped frame from a hand-clap/transient is normal — ignore it.**
  What matters is *sustained* clipping while speaking. Judge on speech, not claps.
- **Level target for STT/voice:** speech peaks ~60-80%, no sustained CLIP.
  ~-31 dB / <20% peak while speaking is too quiet → raise; sustained 100% → lower.
- **Internal laptop mics can have high hardware gain**: one seen here clipped hard
  at 47% pactl volume. Start such mics low (~20-25%) and test up.
- Piped listing loops (`mic | head`) emit `printf: write error: Broken pipe`;
  guard each `printf ... 2>/dev/null || break` inside `while` loops.

## Song identification (songrec, no API key)

`songrec` = open-source Shazam client (queries Shazam servers — fingerprint leaves
the machine, **not** the audio; nothing written to disk when streamed).

- **Reads a PipeWire monitor source DIRECTLY**: `songrec recognize -d <sink>.monitor -j`.
  No ffmpeg/parec/temp-WAV middle step needed — check `songrec recognize -l` for the
  device name. This is far more accurate than mic-through-speaker.
- Capturing the monitor of the *active* sink (see multi-sink note above) makes it
  work regardless of which output the music went to.
- `-i` = Shazam request interval (default 10s); a 30s window ≈ 3 attempts. Bump the
  window for obscure/quiet tracks.
- Empty stdout = no match (correct behavior when nothing is playing — not a bug).

### Gentoo install
songrec ships in the **GURU** overlay, `~amd64`-masked — no ebuild authoring needed,
just `echo 'media-sound/songrec ~amd64' >> /etc/portage/package.accept_keywords/<f>`
then `emerge media-sound/songrec` (~1 pkg if a GTK4 stack is already present).

## Hermes voice mode tie-in

- There is **no `/sound` command**. The audio command is **`/voice`**
  (`/voice`, `/voice tts`, `/voice off`, `/voice status`).
- **`/voice` toggles are session-only** — there is no `voice.enabled` config key, so
  it never persists across restarts (by design, not a save bug). Re-run each session.
- For a persistent hands-free path use the wake word: `wake_word.enabled: true`
  (toggle `/wake`) — always-on "Hey Hermes" across restarts.
- ctrl+b (record key) captures the **default source** → use `mic set` to point it at
  the right mic and `mic test` to confirm level before relying on voice.
- Config lives in `~/.hermes/config.yaml` under `voice:` and `tts:`; read with
  `hermes config get voice` / `hermes config get tts`, set with `hermes config set`.

## Reusable tools built this session

See `references/audio-cli-tools.md` for the full `mic` and `whatsong` scripts
(installed at `~/.local/bin/`) and their command surfaces — copy/adapt rather than
re-deriving the pactl field-parsing and meter each time.
