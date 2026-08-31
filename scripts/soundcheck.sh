#!/usr/bin/env bash
# soundcheck.sh — verify the audio/identify stack karaoke live modes depend on.
#
# Checks, in order:
#   1. pactl present + a default source (mic) and sink (output monitor)
#   2. optional live VU meter from the mic (mic test), so you can confirm level
#   3. songrec present (Shazam client used for --listen/--output/--radio)
#   4. LRCLIB reachable (synced-lyrics API)
#
# Usage:
#   scripts/soundcheck.sh            # non-interactive checks only
#   scripts/soundcheck.sh --meter [secs]   # also run a short mic VU meter
#
# Exit code is non-zero if a REQUIRED capability (pactl, a source) is missing.
set -uo pipefail

METER=""
METER_SECS=4
if [[ "${1:-}" == "--meter" ]]; then
  METER=1
  [[ -n "${2:-}" ]] && METER_SECS="$2"
fi

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }

fail=0

echo "== Audio devices (PipeWire/Pulse via pactl) =="
if ! command -v pactl >/dev/null 2>&1; then
  bad "pactl not found (need pipewire-pulse or pulseaudio) — live modes cannot capture audio"
  fail=1
else
  ok "pactl present"
  src=$(pactl get-default-source 2>/dev/null)
  sink=$(pactl get-default-sink 2>/dev/null)
  if [[ -n "$src" ]]; then ok "default source (mic): $src"; else bad "no default source (mic) — karaoke --listen/--radio will have nothing to hear"; fail=1; fi
  if [[ -n "$sink" ]]; then ok "default sink (output): $sink  (monitor: ${sink}.monitor)"; else warn "no default sink — karaoke --output needs the sink monitor"; fi
fi

if [[ -n "$METER" && -z "${CI:-}" ]]; then
  echo
  echo "== Mic VU meter (${METER_SECS}s) =="
  if command -v mic >/dev/null 2>&1; then
    mic test "$METER_SECS" || warn "mic meter exited non-zero"
  elif command -v parec >/dev/null 2>&1; then
    warn "'mic' helper not on PATH; capturing ${METER_SECS}s with parec instead"
    timeout "$METER_SECS" parec --format=s16le --rate=16000 --channels=1 >/dev/null 2>&1 \
      && ok "captured audio for ${METER_SECS}s (no level display)" \
      || warn "parec capture failed"
  else
    warn "neither 'mic' nor 'parec' available for a level meter"
  fi
fi

echo
echo "== Song identification (songrec / Shazam) =="
if command -v songrec >/dev/null 2>&1; then
  ok "songrec present: $(songrec --version 2>/dev/null | head -1)"
  warn "songrec identifies ONLINE via Shazam; there is no offline audio fingerprint match"
else
  bad "songrec not found (emerge media-sound/songrec) — --listen/--output/--radio disabled"
fi

echo
echo "== Lyrics backend (LRCLIB) =="
LRCLIB_BASE="${LRCLIB_BASE:-https://lrclib.net}"
if command -v curl >/dev/null 2>&1; then
  code=$(curl -s -m 8 -o /dev/null -w '%{http_code}' \
    "$LRCLIB_BASE/api/get?artist_name=R.E.M.&track_name=Losing+My+Religion" 2>/dev/null || echo 000)
  if [[ "$code" == "200" ]]; then ok "LRCLIB reachable ($LRCLIB_BASE)"; else warn "LRCLIB probe returned HTTP $code (offline? cache still serves known songs)"; fi
else
  warn "curl not present; skipping LRCLIB probe"
fi

echo
if [[ "$fail" -ne 0 ]]; then
  bad "sound check FAILED — a required audio capability is missing (see above)"
  exit 1
fi
ok "sound check passed"
