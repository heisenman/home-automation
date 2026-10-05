#!/bin/bash
# Pause the ERV + dehum automations on ha-2, run tools/erv_airflow_test.py, and ALWAYS restore their previous
# enabled state (trap) — a crashed test must not leave the house's ventilation unmanaged.
#   tools/erv_airflow_test.sh [args passed to erv_airflow_test.py]
set -euo pipefail
REPO=/home/visko/home_automation
HA2="ssh -i $HOME/.ssh/id_cluster -o ConnectTimeout=8 visko@192.168.1.210"
cd "$REPO"

put() {   # put <device> <json patch> — admin bearer derived from ha-2's own master (never leaves ha-2)
  $HA2 "cd ~/home_automation && TOK=\$(venv/bin/python -c 'from server.control import secret_store as s; print(s.api_token(s.load_master()))') && curl -sf -X PUT -H \"Authorization: Bearer \$TOK\" -H 'Content-Type: application/json' -d '$2' http://127.0.0.1:8123/control/$1/policy >/dev/null"
}
was() {   # was <device> -> "true"/"false": current enabled flag
  $HA2 "sqlite3 ~/home_automation/instance/db/control.db \"select json from automation_policy where device_id='$1'\"" \
    | python3 -c 'import sys,json; print(str(json.load(sys.stdin).get("enabled", True)).lower())'
}

ERV_WAS=$(was erv_attic); DH_WAS=$(was dehum_attic)
echo "automations before: erv_attic=$ERV_WAS dehum_attic=$DH_WAS — pausing both"
trap 'put erv_attic "{\"enabled\": $ERV_WAS}"; put dehum_attic "{\"enabled\": $DH_WAS}"; echo "restored: erv_attic=$ERV_WAS dehum_attic=$DH_WAS"' EXIT
put erv_attic '{"enabled": false}'
put dehum_attic '{"enabled": false}'
python3 tools/erv_airflow_test.py "$@"
