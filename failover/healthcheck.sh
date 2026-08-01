#!/usr/bin/env bash
# keepalived track_script body. Exit 0 if THIS box is FIT to be dictator, non-zero otherwise.
# Fit = ha-api responding AND the Midea reachable on the LAN. An unfit MASTER loses 'weight'
# priority (see keepalived.conf.tmpl) -> the healthy standby takes over.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
[ -f "$REPO/instance/cluster.env" ] && . "$REPO/instance/cluster.env"
: "${API:=http://localhost:8123}"

# --- latency self-instrumentation (board healthcheck-latency-guard) --------------------------------
# Record how long THIS run took and how it exited, so healthcheck_latency_guard.py can alert BEFORE the
# script crosses keepalived's `timeout` — the 2026-07-27 outage was silent for 4 days precisely because
# nothing watched this number (8.96s against a 4s timeout, so the check could never pass and the air-gap
# VIP could never move).
#
# MUST cost ~nothing: this runs every 5s on every leg, and keepalived's fork/exec churn is already a
# measured power item (board os-idle-churn — ~14 processes per cycle). So: $EPOCHREALTIME is a bash
# builtin (no `date`), printf is a builtin, and the append is one redirect. Zero extra processes.
# The guard truncates the file when it reads; errors are swallowed so instrumentation can NEVER make a
# fitness probe fail (a broken stat file must not take down failover).
_HC_T0=$EPOCHREALTIME
_HC_STAT="$REPO/instance/.healthcheck-latency"
_hc_record() {
    local rc=$?
    printf '%s %s %s\n' "$_HC_T0" "$EPOCHREALTIME" "$rc" >>"$_HC_STAT" 2>/dev/null
    return $rc
}
trap _hc_record EXIT

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

# 1. local ha-api up (proves the stack is alive: uvicorn serving AND the hot DB readable).
#
# MUST be an O(1) probe. This used to hit /api/v1/sensors, which builds the whole PWA sensor view via an
# UNBOUNDED `GROUP BY device_id, metric` scan of every authoritative row (viewmodel.build_sensor_list) — so
# the probe's cost scaled with dataset size. On 2026-07-27 that bit hard: the air-gap standby's hot.db had
# grown to 2.9M rows (no compactor in the ha-ag-* set), /api/v1/sensors hit 8.96s, and keepalived calls this
# script every 5s with a 4s timeout. curl gave up at 4s but uvicorn's anyio worker kept running the 9s query
# to completion (a client disconnect does NOT cancel a sync threadpool call), so work arrived faster than it
# drained -> a core pinned at 100% (+8W at the wall) AND the check could never pass -> weight -60 -> the
# air-gap VIP could no longer move to the standby. A liveness probe silently disarmed failover.
#
# /health is index-backed (MAX(ts) on idx_readings_ts) and proves strictly MORE than the old probe did:
# same "uvicorn is answering", plus the hot DB actually opens and reads. 0.48s vs 8.96s on that same 2.9M-row
# db. Keep any future fitness signal O(1) — never wire this to a view builder.
curl -fsS --max-time 4 "$API/health" >/dev/null 2>&1 || exit 1

# 2. There is deliberately NO device-reachability check here. (Removed 2026-08-01; it used to ping the
# Midea, and the air-gap leg carved itself out of it with `[ "$NET_NAME" = airgap ] && exit 0`.)
#
# WHY, because this is the kind of check that looks obviously useful and keeps getting re-added:
#
#   A fitness probe answers "should the VIP move to my peer?", which is a DIFFERENTIAL question — it is
#   only meaningful if the peer could be better. Device reachability is a SHARED-FATE signal: both nodes
#   ping the same appliance over the same network and get the same answer. So it cannot distinguish "this
#   node is broken" from "the dehumidifier is unplugged", and failing over cannot fix an unplugged
#   dehumidifier — you just hand the VIP to a box that fails the identical check, or flap between them.
#
#   The test for anything added here: IF THIS SIGNAL GOES BAD, WOULD MOVING THE VIP FIX IT? If no, it
#   belongs in monitoring (required-services.yaml without `on_fail: failover`, or a gap-watcher alert),
#   not in this script. Device health is absolutely worth alerting on — just never with the VIP.
#
# It also cost real money to keep: the Midea's IP was DHCP-assigned and had gone stale (env said
# 192.168.0.211, the device had moved to 192.168.1.119), so every 5s cycle burned a 2s `ping -W2` timeout
# — 51% of keepalived's 4s budget — and marked the household dictator permanently unfit. Plus 4 processes
# per cycle (grep|cut|tr + ping) against the fork/exec churn this box is already trying to reduce.
#
# Everything above this line is node-LOCAL (this box's own uvicorn + hot.db, its own service-healer
# marker), which is what makes it differential and therefore a legitimate basis for moving a VIP.
exit 0
