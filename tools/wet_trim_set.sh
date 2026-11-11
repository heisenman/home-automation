#!/usr/bin/env bash
# wet_trim_set.sh TAG on STEP_C | TAG off — set the ERV's wet trim on ha-2 (tools/wet_trim_set.py) from a one-shot
# cron job, then delete every crontab line carrying TAG once the change reads back (so its retry line goes too).
# Hugh, 2026-11-10. Log: instance/dehum-study/wet_trim_set.log
set -euo pipefail
HA2="${HA2:-192.168.1.200}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/instance/dehum-study/wet_trim_set.log"
tag="${1:?TAG}"; shift
if ssh -o BatchMode=yes -o ConnectTimeout=30 "$HA2" "cd home_automation && venv/bin/python - $*" \
     <"$ROOT/tools/wet_trim_set.py" >>"$LOG" 2>&1; then
  crontab -l | grep -vF -- "$tag" | crontab -
  echo "$(date -u +%FT%TZ) cron lines tagged $tag removed" >>"$LOG"
fi
tail -1 "$LOG"
