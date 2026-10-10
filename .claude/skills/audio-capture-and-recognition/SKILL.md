---
name: audio-capture-and-recognition
description: "Capture Linux system output audio and identify what plays."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [audio, pipewire, pulseaudio, pactl, monitor-source, songrec, shazam, capture]
    related_skills: [gentoo-ebuild-maintenance, songsee, music-audio-analysis]
---

# Audio Capture & Recognition (Linux desktop)

Capturing what the MACHINE is playing (not what the microphone hears), and
identifying it. Covers monitor-source capture, song fingerprinting, and the
sharp edges that make naive attempts silently return nothing.

## Trigger conditions

- "What song is playing?" / identify currently-playing audio.
- Record or analyze desktop/system output audio (a stream, a video, a call).
- Any task where piping speaker output into a tool beats mic capture.

## Capturing system output, not the microphone

PulseAudio/PipeWire exposes a `.monitor` source for every sink. Capturing that
gets the clean digital stream — far better than mic-through-speakers, and it
works regardless of speaker volume.

```bash
pactl list short sources | grep monitor   # every capturable monitor
pactl get-default-sink                    # -> append ".monitor"
```

**Check `--help` before building an ffmpeg/parec -> temp-WAV -> tool pipeline.**
Many tools take a monitor source name directly (`songrec -d`, `parec -d`,
`ffmpeg -f pulse -i <source>`). Skipping the intermediate file is simpler AND
means no stray recording of the user's desktop audio lands in `/tmp`.

### Pitfall: on multi-sink systems the DEFAULT sink is often the wrong one

With an internal speaker plus a USB DAC / monitor-with-audio, playback is
frequently routed somewhere other than the default sink. Resolving
`pactl get-default-sink` blindly makes you capture **silence** — which surfaces
as a confusing "no match" or an empty recording rather than a clear error.

Resolve from the ACTIVE playback stream, falling back to the default:

```bash
sink_idx="$(pactl list short sink-inputs | awk 'NR==1{print $2}')"
sink="$(pactl list short sinks | awk -v i="$sink_idx" '$1==i{print $2}')"
[ -n "$sink" ] || sink="$(pactl get-default-sink)"
DEVICE="${sink}.monitor"
```

Known sharp edges in that snippet (do not present it as bulletproof):
- A CORKED/paused sink-input still appears in `sink-inputs` and sends you to a
  silent sink. Filter to RUNNING streams when it matters.
- Column layout of `pactl list short` is pactl-version-dependent; prefer
  `pactl -f json list sink-inputs` where available.
- Monitors idle to `SUSPENDED`. That is normal, not a fault.

### Pitfall: "no match" vs "nothing was playing"

Recognition tools commonly exit **0 with empty stdout** when nothing matched.
Distinguish the cases explicitly or the user cannot tell a miss from a
misconfiguration:
- exit 124 (timeout) -> listened, nothing recognised in time
- exit 0 + empty stdout -> heard audio, no fingerprint match
- pre-check `pactl list short sink-inputs` -> warn "nothing is playing" up
  front instead of burning the whole timeout

## Song identification

`media-sound/songrec` (open-source Shazam client) reads monitor sources
directly — no ffmpeg hop, no temp file. Acquisition, full CLI surface, tuning
and privacy framing: `references/song-identification-linux.md`.

A finished, re-runnable wrapper lives at `scripts/whatsong` — resolves the
active sink, validates the monitor exists, warns when nothing is playing, and
separates timeout from no-match.

**State privacy accurately.** songrec queries Shazam's servers: the
*fingerprint* leaves the machine, the raw audio does not, and nothing is
written to disk when streaming from a monitor. Do NOT call this "offline" —
that overclaims and has to be walked back mid-session.

## Don't conflate the agent's voice recorder with audio tooling

A voice/STT record key captures the **microphone** into speech-to-text. It is
not wired to any audio-analysis tool. Pointing it at music yields a garbled
lyric transcript. When a user asks "should I press <record key> to test?", the
answer is no — direct them to run the actual CLI tool, and say plainly that the
two are unrelated rather than letting them discover it by failing.

## Verification honesty

Device enumeration, `--help`, and `bash -n` do NOT prove an end-to-end
recognition path. If nothing was playing during setup, say so: report it as
"verified structurally; first live run pending" rather than implying the
pipeline is validated.

## References

- `references/song-identification-linux.md` — songrec CLI surface, Gentoo
  acquisition, tuning, alternatives, privacy notes.
- `scripts/whatsong` — the wrapper script, ready to install to `~/.local/bin`.
