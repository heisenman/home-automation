#!/usr/bin/env bash
# erv_night.sh off|high MIN | guard | stop | status — the ERV night pair for the dehumidifier study (tools/erv_night.py,
# run on ha-2). Fired by visko's crontab on .210 (study doc §6b, 2026-11-10). Log: instance/dehum-study/erv_night.log
set -euo pipefail
HA2="${HA2:-192.168.1.200}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/instance/dehum-study"
verb="${*:?off|high MIN, guard, stop or status}"
ssh -o BatchMode=yes -o ConnectTimeout=30 "$HA2" "cd home_automation && venv/bin/python - $verb" \
  <"$ROOT/tools/erv_night.py" >>"$OUT/erv_night.log" 2>&1
tail -1 "$OUT/erv_night.log"
