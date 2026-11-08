#!/usr/bin/env bash
# dehum_block.sh start <end-iso>|stop|status — the dehumidifier study's Aprilaire-only block (tools/dehum_block.py,
# run on ha-2). Fired by visko's crontab on .210 (study doc §6b, 2026-11-07). Log: instance/dehum-study/dehum_blocks.log
set -euo pipefail
HA2="${HA2:-192.168.1.200}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/instance/dehum-study"
verb="${1:?start <end-iso>|stop|status}"
arg=""
if [ "$verb" = start ]; then arg="$(date -d "${2:?end time, e.g. 2026-11-11T18:00-08:00}" +%s)"; fi
ssh -o BatchMode=yes -o ConnectTimeout=30 "$HA2" "cd home_automation && venv/bin/python - $verb $arg" \
  <"$ROOT/tools/dehum_block.py" >>"$OUT/dehum_blocks.log" 2>&1
tail -1 "$OUT/dehum_blocks.log"
