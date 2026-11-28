#!/usr/bin/env bash
# Open the karaoke dashboard (:8900) as a standalone Chrome app window.
#
# --app gives a window without tabs/omnibox in which Chrome does not reserve
# Ctrl+T/N/W etc.; the dashboard's appShortcuts.js swallows them so they stay
# in the app. A dedicated --user-data-dir makes it its own browser process,
# so it gets its own window class/dock icon (StartupWMClass in the .desktop
# file) and never piggy-backs on an already-open everyday Chrome window.
set -euo pipefail

URL="${KARAOKE_DASHBOARD_URL:-http://127.0.0.1:8900/}"
PROFILE="${KARAOKE_APP_PROFILE:-$HOME/.config/karaoke-dashboard-app}"
CLASS="${KARAOKE_APP_CLASS:-karaoke-dashboard}"
CHROME="${CHROME:-}"
if [[ -z "$CHROME" ]]; then
  for c in google-chrome-stable google-chrome chromium chromium-browser; do
    if command -v "$c" >/dev/null 2>&1; then CHROME="$c"; break; fi
  done
fi
[[ -n "$CHROME" ]] || { echo "karaoke-dashboard-app: no Chrome/Chromium found" >&2; exit 1; }

# Bring the backend up if it isn't (systemd --user unit from `make systemd-install`).
health="${URL%/}/api/health"
if ! curl -fsS -m 2 "$health" >/dev/null 2>&1; then
  systemctl --user start karaoke-dashboard.service >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do
    curl -fsS -m 2 "$health" >/dev/null 2>&1 && break
    sleep 1
  done
fi

exec "$CHROME" \
  --user-data-dir="$PROFILE" \
  --class="$CLASS" \
  --no-first-run \
  --no-default-browser-check \
  --app="$URL" \
  "$@"
