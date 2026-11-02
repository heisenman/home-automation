#!/usr/bin/env bash
# ha2_report.sh <tools/script.py> <name> — run a read-only, stdlib-only analysis script ON ha-2 (piped, so ha-2
# needs no tools/ deploy) and file its markdown on .210 under instance/<name>/reports/<date>.md + latest.md.
# Generic sibling of dehum_readiness_report.sh (which also extracts a status line). Fired by visko's crontab.
set -euo pipefail
HA2="${HA2:-192.168.1.200}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
script="${1:?script}"; name="${2:?name}"
OUT="$ROOT/instance/$name/reports"
mkdir -p "$OUT"
f="$OUT/$(TZ=America/Los_Angeles date +%F).md"
if ssh -o BatchMode=yes -o ConnectTimeout=30 "$HA2" 'cd home_automation && nice -n 15 python3 -' \
     <"$ROOT/$script" >"$f.tmp" 2>"$OUT/last-error.log"; then
  mv "$f.tmp" "$f"
  ln -sfn "$(basename "$f")" "$OUT/latest.md"
  echo "report: $f"
else
  rc=$?
  rm -f "$f.tmp"
  echo "$(date -u -Is) FAILED rc=$rc: $(tail -1 "$OUT/last-error.log")" | tee -a "$OUT/failures.log" >&2
  exit "$rc"
fi
