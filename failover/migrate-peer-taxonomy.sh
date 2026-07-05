#!/usr/bin/env bash
# failover/migrate-peer-taxonomy.sh
#
# Replay the ADR-0026 canonical-area taxonomy + device rename/relocate ON THE PEER (.245) so its registry
# and history match the dictator (.210). Until this runs, .245 keeps ingesting under the OLD registry and
# reconcile-history resurrects those old ids/areas back into .210.
#
# RUN ON .245 (the peer), after `git pull`. Idempotent — safe to re-run. Pass --dry-run to preview.
#
# PREREQS — copy these gitignored inputs from .210 into instance/ first (they never enter git):
#     instance/areas.yaml            (canonical room registry)
#     instance/area-migration.yaml   (Phase-2 area crosswalk)
#     instance/device-rename.yaml    (Phase-3 rename+relocate worksheet)
#   e.g. from .210:  scp instance/{areas,area-migration,device-rename}.yaml visko@192.168.0.245:~/home_automation/instance/
#
# NOTE on actuators: the dictator carries `device_type:` markers in control.yaml so its controller resolves
# the Midea/Levoit ids by type. The peer's ha-controller is inactive (standby), so it does not seed actuator
# policies — the data + registry rename is what stops the reconcile resurrection, which is this script's job.
set -euo pipefail
cd "$(dirname "$0")/.."

DRY=""
[[ "${1:-}" == "--dry-run" ]] && DRY="--dry-run"

# Safety: this is the PEER-side replay. The dictator (ha-dev / .210) was migrated in-session — don't aim here.
if [[ "$(hostname)" == "ha-dev" ]]; then
  echo "REFUSING: host is ha-dev (the .210 dictator, already migrated). Run this on the .245 peer."; exit 1
fi

missing=0
for f in instance/areas.yaml instance/area-migration.yaml instance/device-rename.yaml; do
  [[ -f "$f" ]] || { echo "MISSING $f — scp it from .210 (gitignored) first."; missing=1; }
done
[[ $missing -eq 0 ]] || { echo "Copy the inputs above, then re-run."; exit 1; }

echo "========== Phase 2: canonical area crosswalk =========="
venv/bin/python -m server.maintenance.device_relocate crosswalk instance/area-migration.yaml --no-peer $DRY

echo "========== Phase 3: device rename + relocate =========="
venv/bin/python -m server.maintenance.apply_rename_worksheet --no-peer $DRY

echo "========== verify =========="
venv/bin/python -m tests.test_areas || true
if [[ -n "$DRY" ]]; then
  echo "DRY-RUN complete — re-run without --dry-run to apply."
else
  echo "DONE. Idempotent — safe to re-run. Old ids should no longer be generated on this box."
fi
