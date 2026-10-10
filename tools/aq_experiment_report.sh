#!/usr/bin/env bash
# aq_experiment_report.sh — run the AQ v2 shadow-experiment report ON ha-2 (where the gas data lives) and file
# it on .210 under instance/aq-experiment/reports/<date>.md (+ latest.md). Fired daily by
# systemd/ha-aq-experiment-report.timer. Read-only on ha-2; see tools/aq_experiment_report.py.
set -euo pipefail
HA2="${HA2:-192.168.1.200}"
OUT="$(cd "$(dirname "$0")/.." && pwd)/instance/aq-experiment/reports"
mkdir -p "$OUT"
f="$OUT/$(date -u +%F).md"
if ssh -o BatchMode=yes -o ConnectTimeout=30 "$HA2" \
     'cd home_automation && nice -n 15 venv/bin/python tools/aq_experiment_report.py' >"$f.tmp" 2>"$OUT/last-error.log"; then
  mv "$f.tmp" "$f"
  ln -sfn "$(basename "$f")" "$OUT/latest.md"
  echo "report: $f ($(grep -c '^- ' "$f") bullet lines)"
else
  rc=$?
  rm -f "$f.tmp"
  echo "$(date -u -Is) FAILED rc=$rc: $(tail -1 "$OUT/last-error.log")" | tee -a "$OUT/failures.log" >&2
  exit "$rc"
fi
