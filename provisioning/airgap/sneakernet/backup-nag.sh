#!/usr/bin/env bash
# Sneakernet backup nag (ADR-0033 Phase 4) — the air-gap dictator's ONLY off-box copy is the periodic USB
# backup, so nag if it's overdue. Fires home/_alert/new (rides the alert/ntfy path) when the last backup is
# older than the threshold. Run via ha-sneakernet-nag.timer (weekly).
#   backup-nag.sh [max-days]     # default 10
set -euo pipefail
MAX_DAYS="${1:-10}"
ROOT="${HA_ROOT:-$HOME/home_automation}"
MARK="$ROOT/instance/.last-sneakernet-backup"
BROKER="${HA_BROKER:-127.0.0.1}"

if [ -f "$MARK" ]; then
  LAST=$(date -d "$(cat "$MARK")" +%s 2>/dev/null || echo 0)
  AGE=$(( ( $(date +%s) - LAST ) / 86400 ))
else
  AGE=9999
fi
echo "last sneakernet backup: ${AGE}d ago (threshold ${MAX_DAYS}d)"

if [ "$AGE" -gt "$MAX_DAYS" ]; then
  MSG="Air-gap backup overdue — last sneakernet backup ${AGE}d ago (>${MAX_DAYS}d). Plug in the USB and run provisioning/airgap/sneakernet/backup.sh."
  echo "ALERT: $MSG"
  command -v mosquitto_pub >/dev/null 2>&1 && mosquitto_pub -h "$BROKER" -t 'home/_alert/new' \
    -m "{\"kind\":\"backup_overdue\",\"severity\":\"warning\",\"device_id\":\"_system\",\"name\":\"Air-gap backup\",\"detail\":\"$MSG\"}" \
    2>/dev/null || true
fi
