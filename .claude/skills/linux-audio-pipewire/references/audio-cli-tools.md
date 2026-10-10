# Audio CLI tools (mic, whatsong)

Two Bash tools built and verified on a Gentoo/PipeWire box (installed to
`~/.local/bin/`, which was already on PATH). Copy/adapt; they encode the
pactl/parec quirks from the umbrella SKILL.md so you don't re-derive them.

## `whatsong` — identify the currently-playing song

Streams the monitor of the **active** output sink into `songrec`. No temp file.

Key logic:
- Resolve sink from active `sink-inputs` stream, fall back to default sink.
- Verify the `.monitor` source exists (`pactl list short sources`) before handing
  to songrec — gives a clear error instead of a silent no-match.
- Warn (but still listen) when no `sink-inputs` are active.
- `timeout N songrec recognize -d "$DEVICE" [-j]`; empty stdout OR exit 124 = no
  match. `-t` flag adjusts the window; default 30s.

```bash
#!/usr/bin/env bash
set -uo pipefail
TIMEOUT=30; DEVICE=""; JSON=""
die(){ printf 'whatsong: %s\n' "$1" >&2; exit 1; }
while [[ $# -gt 0 ]]; do case "$1" in
  -j|--json) JSON="-j"; shift ;;
  -d|--device) DEVICE="${2:-}"; shift 2 ;;
  -t|--timeout) TIMEOUT="${2:-}"; shift 2 ;;
  -l|--list) exec songrec recognize --list-devices ;;
  -h|--help) grep -n . "$0" | head -1 >/dev/null; sed -n '2,15p' "$0"; exit 0 ;;
  *) die "unknown option: $1" ;;
esac; done
command -v songrec >/dev/null || die "songrec not installed"
if [[ -z $DEVICE ]]; then
  idx="$(pactl list short sink-inputs 2>/dev/null | awk 'NR==1{print $2}')"
  sink=""
  [[ -n $idx ]] && sink="$(pactl list short sinks 2>/dev/null | awk -v i="$idx" '$1==i{print $2}')"
  [[ -z $sink ]] && sink="$(pactl get-default-sink 2>/dev/null)"
  [[ -n $sink && $sink != "@DEFAULT_SINK@" ]] || die "no output sink; use -d"
  DEVICE="${sink}.monitor"
fi
pactl list short sources 2>/dev/null | awk '{print $2}' | grep -qxF "$DEVICE" \
  || die "monitor not found: $DEVICE (try -l)"
pactl list short sink-inputs 2>/dev/null | grep -q . \
  || printf 'whatsong: nothing playing - listening anyway...\n' >&2
printf 'Listening on %s (up to %ss)...\n' "${DEVICE##*.HiFi__}" "$TIMEOUT" >&2
out="$(timeout "$TIMEOUT" songrec recognize -d "$DEVICE" $JSON 2>/dev/null)"; rc=$?
[[ -n ${out//[[:space:]]/} ]] && { printf '%s\n' "$out"; exit 0; }
[[ $rc -eq 124 ]] && die "no match within ${TIMEOUT}s - volume up? try longer -t"
die "no match (Shazam didn't recognise it, or too quiet/obscure)."
```

## `mic` — inspect / select / test microphones

Subcommands: `status` (default) · `list` · `set <id|substring>` · `vol <0-150>` ·
`up/down [step]` · `mute/unmute/toggle` · `test [secs] [-d src]`.

Critical implementation details (all learned the hard way this session):
- **List loop parses TAB fields**: `while IFS=$'\t' read -r idx name driver spec state`.
  Whitespace-split puts "2ch" in the STATE column.
- **`resolve_src`** accepts numeric index, exact name, or unique case-insensitive
  substring (prefer non-monitor); die on ambiguous match.
- **`label()`** shortens: `*.HiFi__X_source`→`X` (strip trailing `_`),
  `alsa_input.usb-*`→`USB <model>`, `*.monitor`→`monitor:*`.
- **VU meter** = the parec|python snippet in SKILL.md; `\033[K` clear-to-EOL each
  redraw; treat `timeout` rc 124 as success; count clipped frames, report at end.
- **Guard piped output**: `printf ... 2>/dev/null || break` in listing `while`
  loops so `mic | head` doesn't spew "Broken pipe".
- Refuse `vol > 150` (clipping guard).

The meter's guidance strings encode the level target: "aim to peak green/yellow,
not red"; on finish, if clipped → "lower the mic volume (mic down)", else "if the
bar barely moved while speaking, raise it (mic up)".

## Environment notes (this box)
- Internal mic `Mic2` (`alsa_input.pci-*.HiFi__Mic2__source`) has aggressive HW
  gain: clipped hard at 47% pactl volume; ~20-25% is a sane start.
- Two sinks present (internal Speaker + USB HP 527pm) → the active-sink resolution
  in `whatsong` matters here specifically.
