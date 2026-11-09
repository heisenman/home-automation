#!/usr/bin/env bash
# erv_min.sh start|stop|status — hold the ERV at minimum for a dehumidifier-study window (tools/erv_min.py, run on
# ha-2). Fired by visko's crontab on .210 (study doc §6b, 2026-11-09). Log: instance/dehum-study/erv_min.log
set -euo pipefail
HA2="${HA2:-192.168.1.200}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/instance/dehum-study"
verb="${1:?start|stop|status}"
ssh -o BatchMode=yes -o ConnectTimeout=30 "$HA2" "cd home_automation && venv/bin/python - $verb" \
  <"$ROOT/tools/erv_min.py" >>"$OUT/erv_min.log" 2>&1
tail -1 "$OUT/erv_min.log"
