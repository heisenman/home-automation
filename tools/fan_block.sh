#!/usr/bin/env bash
# fan_block.sh start|check|stop|hold|release — the dehumidifier study's fan-only zero blocks (tools/fan_block.py, run on ha-2).
# Fired by visko's crontab on .210 at 10:00 / 15:00 PT (start) +30 min (check: end early unless conditions
# still permit), +45 min (stop) and +55 min (stop retry).
# Stops scheduling itself once the readiness report's HVAC zero-model criterion passes (study doc §6c).
set -euo pipefail
HA2="${HA2:-192.168.1.200}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/instance/dehum-study"
verb="${1:?start|check|stop|hold|release}"
passed() { python3 - "$OUT/reports/status.json" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
g = next(g for g in s["gates"] if g["id"] == "G2a")
sys.exit(0 if any(c["name"].startswith("HVAC pair zero model") and c["ok"] for c in g["criteria"]) else 1)
PY
}
# hold mode (fan ON continuously, Hugh 2026-11-07) ends itself: once the criterion passes, hold becomes release
if [ "$verb" = hold ] && passed; then verb=release; fi
if [ "$verb" = start ] && passed
then
  echo "{\"ts\": \"$(date -u +%FT%TZ)\", \"msg\": \"skip: HVAC zero model already passes\"}" >>"$OUT/fan_blocks.log"
  exit 0
fi
ssh -o BatchMode=yes -o ConnectTimeout=30 "$HA2" "cd home_automation && venv/bin/python - $verb" \
  <"$ROOT/tools/fan_block.py" >>"$OUT/fan_blocks.log" 2>&1
