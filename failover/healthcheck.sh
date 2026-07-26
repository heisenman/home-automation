#!/usr/bin/env bash
# keepalived track_script body. Exit 0 if THIS box is FIT to be dictator, non-zero otherwise.
# Fit = ha-api responding AND the Midea reachable on the LAN. An unfit MASTER loses 'weight'
# priority (see keepalived.conf.tmpl) -> the healthy standby takes over.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
[ -f "$REPO/instance/cluster.env" ] && . "$REPO/instance/cluster.env"
: "${API:=http://localhost:8123}"

# 0. Maintenance inhibit. While a FRESH flag exists, report FIT so a DELIBERATE ha-api restart (the
# device-admin orchestration's fleet-restart, or a cutover restart) can't drop priority -> spurious
# failover (the failover-primitives lesson: don't let an op flap the VIP it runs under). Staleness-bounded
# so a leftover flag (crashed op) can't blind failover past MAINT_FIT_MAX_AGE. Set/cleared by
# apply_rename_worksheet.run_plan around the restart; a manual restart can `touch instance/.maintenance-fit`
# then remove it. Inert when absent — normal health logic below is unchanged.
: "${MAINT_FIT_MAX_AGE:=300}"
FIT_FLAG="$REPO/instance/.maintenance-fit"
if [ -f "$FIT_FLAG" ]; then
  _age=$(( $(date +%s) - $(stat -c %Y "$FIT_FLAG" 2>/dev/null || echo 0) ))
  [ "$_age" -ge 0 ] && [ "$_age" -le "$MAINT_FIT_MAX_AGE" ] && exit 0
fi

# 0b. Service-completeness UNFIT (plan crystalline-spinning-spindle, Stage C). The service healer writes and
# REFRESHES instance/.unfit while a FAILOVER-WORTHY required unit has stayed down past its threshold (self-heal
# + peer-repair both failed). A FRESH marker -> this box is unfit to be dictator even though ha-api answers
# (the 2026-07-26 case: :8123 up, but the front-end/controller dead), so the standby takes over. Applies on
# ALL nets incl. air-gap. A STALE marker (healer stopped refreshing) is IGNORED so we never fail over on a
# dead signal; maintenance-inhibit above still overrides (deliberate ops don't trip failover).
: "${UNFIT_FRESH_S:=90}"
UNFIT_FLAG="$REPO/instance/.unfit"
if [ -f "$UNFIT_FLAG" ]; then
  _uage=$(( $(date +%s) - $(stat -c %Y "$UNFIT_FLAG" 2>/dev/null || echo 0) ))
  [ "$_uage" -ge 0 ] && [ "$_uage" -le "$UNFIT_FRESH_S" ] && exit 1
fi

# 1. local ha-api up (proves the stack is alive)
curl -fsS --max-time 4 "$API/api/v1/sensors" >/dev/null 2>&1 || exit 1

# Air-gap dictator: no household actuators to reach (its actuator set grows as devices migrate) —
# fit on API alone, else it'd be wrongly marked unfit for a .0.x Midea it can't reach. (DJ-16)
[ "${NET_NAME:-}" = airgap ] && exit 0

# 2. Midea reachable on the LAN (can we actually actuate?)
ENVF="$REPO/instance/midea-device.env"
if [ -f "$ENVF" ]; then
  MIP=$(grep -E '^MIDEA_IP=' "$ENVF" | head -1 | cut -d= -f2- | tr -d "\"' ")
  if [ -n "$MIP" ]; then ping -c1 -W2 "$MIP" >/dev/null 2>&1 || exit 2; fi
fi
exit 0
