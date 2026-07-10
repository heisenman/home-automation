#!/usr/bin/env bash
# Verify (and optionally restore) a sneakernet backup (ADR-0033 Phase 4).
#   verify-restore.sh <backup-dir>                 # verify integrity only
#   verify-restore.sh <backup-dir> --restore <ha-root>   # verify THEN restore into a target repo
# Restore is refused unless the manifest verifies. Never overwrites without an explicit --restore.
set -euo pipefail
BK="${1:?usage: verify-restore.sh <backup-dir> [--restore <ha-root>]}"
[ -f "$BK/MANIFEST.sha256" ] || { echo "no MANIFEST.sha256 in $BK — not a backup"; exit 1; }

echo "== verifying $BK =="
( cd "$BK" && sha256sum -c --quiet MANIFEST.sha256 ) || { echo "INTEGRITY FAILURE — refusing"; exit 2; }
echo "OK: integrity verified ($(grep -c . "$BK/MANIFEST.sha256") files)"

if [ "${2:-}" = "--restore" ]; then
  ROOT="${3:?--restore needs a target ha-root}"
  echo "== restoring into $ROOT (dbs + parquet + config) =="
  mkdir -p "$ROOT/instance/db"
  [ -d "$BK/db" ] && rsync -a "$BK/db/" "$ROOT/instance/db/" && echo "  restored db/"
  for f in devices.yaml weather.env control.yaml; do
    [ -f "$BK/instance/$f" ] && install -D -m 0644 "$BK/instance/$f" "$ROOT/instance/$f" && echo "  restored instance/$f"
  done
  echo "OK: restored. Restart ingest services to pick it up."
fi
