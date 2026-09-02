#!/usr/bin/env bash
# Copy the local SQLite karaoke library into the cluster PVC.
#
# The library API serves a point-in-time snapshot of the host database. Re-run
# this script after indexing new tracks locally to refresh what the cluster
# serves.
#
# Uses `sqlite3 .backup` when available so the copy is consistent even if the
# database is being written to; falls back to a plain copy otherwise.
set -euo pipefail

CONTEXT="${KUBE_CONTEXT:-kind-karaoke}"
NAMESPACE="${K8S_NAMESPACE:-karaoke}"
DATA_DIR="${KARAOKE_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/karaoke}"
DB_PATH="${DB_PATH:-$DATA_DIR/karaoke.db}"

if [[ ! -f "$DB_PATH" ]]; then
  echo "error: database not found at $DB_PATH" >&2
  exit 1
fi

POD="$(kubectl --context "$CONTEXT" -n "$NAMESPACE" \
  get pod -l app.kubernetes.io/name=karaoke-api \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"

if [[ -z "$POD" ]]; then
  echo "error: no karaoke-api pod found in namespace $NAMESPACE" >&2
  echo "hint: run 'make k8s-deploy' first" >&2
  exit 1
fi

TMP_DB="$(mktemp -t karaoke-seed-XXXXXX.db)"
trap 'rm -f "$TMP_DB"' EXIT

if command -v sqlite3 >/dev/null 2>&1; then
  echo "==> Creating consistent snapshot of $DB_PATH"
  sqlite3 "$DB_PATH" ".backup '$TMP_DB'"
else
  echo "==> sqlite3 not found; falling back to plain copy"
  cp "$DB_PATH" "$TMP_DB"
fi

SIZE="$(du -h "$TMP_DB" | cut -f1)"
echo "==> Copying snapshot ($SIZE) into $NAMESPACE/$POD:/data/karaoke.db"
kubectl --context "$CONTEXT" -n "$NAMESPACE" cp "$TMP_DB" "$POD:/data/karaoke.db"

echo "==> Verifying track count as served by the API"
kubectl --context "$CONTEXT" -n "$NAMESPACE" exec "$POD" -- \
  python -c "
import sqlite3
conn = sqlite3.connect('/data/karaoke.db')
print('tracks: ', conn.execute('SELECT COUNT(*) FROM tracks').fetchone()[0])
print('sources:', conn.execute('SELECT COUNT(*) FROM sources').fetchone()[0])
print('lyrics: ', conn.execute('SELECT COUNT(*) FROM lyrics').fetchone()[0])
"

echo "==> Seed complete"
