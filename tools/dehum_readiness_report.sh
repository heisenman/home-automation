#!/usr/bin/env bash
# dehum_readiness_report.sh — run the dehumidifier-study readiness report ON ha-2 (piped; read-only) and file it on
# .210 under instance/dehum-study/reports/: <date>.md + latest.md, status.json (machine-readable gate status) and
# status-history.jsonl (one line per run). Fired daily by visko's crontab on .210 (see the study doc §6c);
# same shape as aq_experiment_report.sh. Code-driven so the study stays on schedule without a session.
set -euo pipefail
HA2="${HA2:-192.168.1.200}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/instance/dehum-study/reports"
mkdir -p "$OUT"
f="$OUT/$(TZ=America/Los_Angeles date +%F).md"
if ssh -o BatchMode=yes -o ConnectTimeout=30 "$HA2" 'cd home_automation && nice -n 15 python3 -' \
     <"$ROOT/tools/dehum_readiness.py" >"$f.tmp" 2>"$OUT/last-error.log"; then
  mv "$f.tmp" "$f"
  ln -sfn "$(basename "$f")" "$OUT/latest.md"
  sed -n '1s/^<!-- status: \(.*\) -->$/\1/p' "$f" >"$OUT/status.json"
  python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print(json.dumps({"generated": s["generated"], "current": s["current"], "gates": {g["id"]: [g["met"], g["of"], g["complete"], g["behind"]] for g in s["gates"]}}))' \
    "$OUT/status.json" >>"$OUT/status-history.jsonl"
  echo "report: $f (current gate $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["current"])' "$OUT/status.json"))"
else
  rc=$?
  rm -f "$f.tmp"
  echo "$(date -u -Is) FAILED rc=$rc: $(tail -1 "$OUT/last-error.log")" | tee -a "$OUT/failures.log" >&2
  exit "$rc"
fi
