#!/usr/bin/env bash
# keepalived notify dispatcher — binds ha-controller to VRRP state so exactly ONE box (the VIP
# holder / MASTER) ever runs the controller.
#   keepalived calls: notify.sh <INSTANCE|GROUP> <name> <STATE> [priority]   (STATE in $3)
#   manual test:      notify.sh MASTER     (STATE in $1)
# MASTER  -> FENCE the peer (stop its controller), verify secrets, start OUR controller + relay-coordinator.
# BACKUP  -> stop OUR controller + relay-coordinator (step down — Core-Rule auto-demote path under preempt).
# FAULT   -> stop OUR controller + relay-coordinator (we're unfit).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
[ -f "$REPO/instance/cluster.env" ] && . "$REPO/instance/cluster.env"
: "${CONTROLLER_UNIT:=ha-controller}"; : "${RELAY_COORD_UNIT:=ha-relay-coordinator}"; : "${PEER_HOST:=}"; : "${CLUSTER_KEY:=$HOME/.ssh/id_cluster}"; : "${CLUSTER_SSH_PORT:=22}"
# Unit names are parameterized (defaults = the household set) so a SECOND, co-resident cluster instance on
# the same host — e.g. an air-gap failover sharing a box with a dev dictator — restarts ITS OWN units on a
# VRRP transition and never the other stack's. Set API_UNIT/EDGE_MAPPER_UNIT in that instance's cluster.env.
: "${API_UNIT:=ha-api}"; : "${EDGE_MAPPER_UNIT:=ha-edge-mapper}"
# The PEER's controller unit — used by the MASTER fence. Defaults to ours (symmetric clusters); set it
# explicitly when the two boxes name the unit differently (e.g. ha-2's ha-controller vs the co-resident .210
# air-gap failover's ha-ag-controller) so a fence never stops the WRONG stack's controller.
: "${PEER_CONTROLLER_UNIT:=$CONTROLLER_UNIT}"
# ADR-0033 failover-ssh-decouple part 2: the control-path fence can travel two transports during cutover.
#   FENCE_MODE=bus   -> signed fence over the cluster bus (ha/cluster/fence); NO SSH (the end state, gated on
#                       the drill proving a bus fence actually stops the peer + closing 22 on wlp2s0).
#   FENCE_MODE=ssh   -> legacy `ssh peer 'systemctl stop'` only.
#   FENCE_MODE=both  -> publish the bus fence AND run the SSH fence (DEFAULT — belt+suspenders; never lose
#                       fencing capability while the bus path is being proven). Flip to `bus` post-drill.
: "${FENCE_MODE:=both}"
: "${PEER_FENCE_HOST:=}"                          # the peer listener's FENCE_HOST identity (its hostname); empty -> bus fence skipped
: "${FENCE_KEY_FILE:=$REPO/instance/.fence_key}"; : "${FENCE_BROKER:=127.0.0.1}"; : "${FENCE_PORT:=1883}"
# The fence must NOT ride the VIP broker — the VIP MOVES to the new master during the very failover the fence
# handles, so a subscriber on the VIP is mid-reconnect when the fence fires (drill 2026-07-10). Publish to the
# PEER's REAL broker (stable), and each listener subscribes to its own LOCAL broker. Defaults keep back-compat.
: "${FENCE_PEER_BROKER:=$FENCE_BROKER}"
: "${VIP:=192.168.0.200}"; : "${BACKUP_GRACE:=4}"   # see BACKUP branch — startup-transient suppression
LOG=/var/log/ha-failover.log
STATE="${3:-${1:-}}"

log(){ printf '%s notify[%s] %s\n' "$(date -Is)" "$STATE" "$*" | tee -a "$LOG" 2>/dev/null || true; }
peer_ssh(){ ssh -p "${CLUSTER_SSH_PORT:-22}" -i "$CLUSTER_KEY" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "visko@$PEER_HOST" "$@"; }
# ADR-0015 #9: restart ha-api so its control plane re-evaluates the VIP gate (mounts on the dictator,
# stays read-only on the standby). Best-effort, active-only, never blocks the VRRP transition.
api_remount(){ systemctl is-active --quiet "$API_UNIT" && { sudo systemctl restart "$API_UNIT" 2>/dev/null && log "$API_UNIT restarted ($1) — control plane re-evaluates VIP gate" || log "$API_UNIT restart skipped/failed (non-fatal)"; }; return 0; }
# ADR-0015 Phase B: bind ha-relay-coordinator to VRRP role so ONLY the dictator signs+publishes relay
# allowlists (one-writer invariant, like the controller). MASTER (re)starts it AFTER the edge-mapper recompute
# so it re-evaluates coverage from THIS box's reach; demotion/fault stops it. Best-effort, non-blocking; clean
# noop where the unit isn't installed. The unit is also HA_VIP-guarded as an independent backstop.
relay_coord(){ # $1=start|stop  $2=state-label
  systemctl cat "$RELAY_COORD_UNIT" >/dev/null 2>&1 || { log "$RELAY_COORD_UNIT not installed — skipping ($2)"; return 0; }
  case "$1" in
    start) sudo systemctl restart "$RELAY_COORD_UNIT" 2>/dev/null && log "$RELAY_COORD_UNIT (re)started ($2) — re-evaluating relay allowlists from local reach" || log "$RELAY_COORD_UNIT start skipped/failed (non-fatal)";;
    stop)  sudo systemctl stop "$RELAY_COORD_UNIT" 2>/dev/null && log "$RELAY_COORD_UNIT stopped ($2) — standby never publishes" || log "$RELAY_COORD_UNIT already stopped/absent";;
  esac
  return 0
}
# ADR-0016: reconcile sensor history with the peer over the cluster back-channel after a role change — the
# new dictator backfills the hole from the (now-reachable) peer; the demoted box hands over its last rows.
# ASYNC + best-effort: a dead peer is fine (idempotent merge, self-corrects on the next proactive pass) and
# this must NEVER block the VRRP transition — control failover cannot wait on data-plane catch-up.
reconcile_history(){ # $1=state-label
  [ -x "$REPO/failover/reconcile-history.sh" ] || return 0
  ( "$REPO/failover/reconcile-history.sh" --once >/dev/null 2>&1 && log "history reconcile fired ($1)" || true ) &
  return 0
}
# ADR-0033 part 2: publish a SIGNED fence to the peer over the cluster bus (no SSH). The peer's
# ha-fence-listener verifies HMAC+freshness+target and stops its OWN controller. Best-effort, non-blocking:
# a failure just means the bus is unreachable — the SSH fence (FENCE_MODE=both) or a dead peer covers it.
bus_fence(){
  [ -f "$FENCE_KEY_FILE" ] || { log "bus-fence skipped: no fence key ($FENCE_KEY_FILE)"; return 0; }
  [ -n "$PEER_FENCE_HOST" ] || { log "bus-fence skipped: PEER_FENCE_HOST unset"; return 0; }
  if FENCE_KEY_FILE="$FENCE_KEY_FILE" FENCE_BROKER="$FENCE_PEER_BROKER" FENCE_PORT="$FENCE_PORT" FENCE_HOST="$(hostname)" \
       python3 "$HERE/cluster-ssh/fence.py" publish --target "$PEER_FENCE_HOST" --unit "$PEER_CONTROLLER_UNIT" >/dev/null 2>&1; then
    log "bus-fence published -> target=$PEER_FENCE_HOST unit=$PEER_CONTROLLER_UNIT via $FENCE_PEER_BROKER"
  else
    log "bus-fence publish failed (non-fatal)"
  fi
  return 0
}

case "$STATE" in
  MASTER)
    log "becoming MASTER -> fence peer (mode=$FENCE_MODE), then start controller"
    # FENCE first: best-effort stop the peer's controller (covers alive-but-not-master / split-brain heal).
    # A failure here means the peer is down/unreachable — which is fine, a dead peer isn't controlling.
    # Bus transport (signed, no SSH) runs for FENCE_MODE=bus|both; SSH transport for ssh|both.
    case "$FENCE_MODE" in
      bus|both) bus_fence ;;
    esac
    if [ "$FENCE_MODE" != bus ] && [ -n "$PEER_HOST" ]; then
      if peer_ssh "sudo systemctl stop $PEER_CONTROLLER_UNIT" 2>/dev/null; then log "fenced peer $PEER_HOST ($PEER_CONTROLLER_UNIT stopped via ssh)"; else log "peer ssh-fence failed/unreachable (ok if peer is down)"; fi
    fi
    # Never start a controller that can't build its issuer.
    if [ ! -f "$REPO/instance/.master_pass" ]; then log "ABORT: missing instance/.master_pass — NOT starting controller"; exit 1; fi
    if sudo systemctl start "$CONTROLLER_UNIT"; then log "controller STARTED — this box is now the dictator"; else log "ERROR: failed to start $CONTROLLER_UNIT"; exit 1; fi
    # ADR-0015 §3: recompute relay coverage from THIS box's own reach (the new dictator hears a different
    # set). Replicate-to-seed (sync-standby brought mesh.db); recompute-to-be-correct (restart the mapper).
    # Best-effort, active-only, never blocks the takeover.
    if systemctl is-active --quiet "$EDGE_MAPPER_UNIT"; then
      sudo systemctl restart "$EDGE_MAPPER_UNIT" 2>/dev/null && log "$EDGE_MAPPER_UNIT restarted — recomputing coverage from local reach" || log "$EDGE_MAPPER_UNIT restart skipped/failed (non-fatal)"
    fi
    relay_coord start MASTER   # this box now signs+publishes relay allowlists (re-eval after coverage recompute)
    api_remount MASTER   # mount the control plane now that this node holds the VIP
    reconcile_history MASTER   # async: backfill the history hole from the peer (best-effort, non-blocking)
    ;;
  BACKUP)
    # Distinguish a GENUINE demotion (yield to primary) from the keepalived START-UP TRANSIENT: at boot
    # keepalived fires BACKUP first, then wins the election and fires MASTER ~1-3s later. The naive
    # "BACKUP -> stop" caused a ~4s control blip on the primary every keepalived (re)start. Wait a short
    # grace, then re-check the VIP: if we now HOLD it, MASTER has taken over here -> this was the boot
    # transient, NOT a demotion -> leave the controller alone. No VIP after grace == real demotion -> stop.
    sleep "$BACKUP_GRACE"
    if ip -o addr show 2>/dev/null | grep -qw "$VIP"; then
      log "VIP $VIP present after ${BACKUP_GRACE}s grace -> startup transient (MASTER active here); NOT stopping controller"
      exit 0
    fi
    log "no VIP after ${BACKUP_GRACE}s grace -> genuine demotion; stopping controller (yield to primary)"
    if sudo systemctl stop "$CONTROLLER_UNIT" 2>/dev/null; then log "controller STOPPED"; else log "controller already stopped"; fi
    relay_coord stop BACKUP   # stop publishing relay allowlists — only the dictator may
    api_remount BACKUP   # unmount the control plane — we no longer hold the VIP
    reconcile_history BACKUP   # async: hand our last-reign rows to the new dictator (best-effort, non-blocking)
    ;;
  FAULT)
    # Health check failed: we are genuinely unfit. Stop immediately — no grace (don't keep actuating while broken).
    log "becoming FAULT -> stop controller immediately (unfit)"
    if sudo systemctl stop "$CONTROLLER_UNIT" 2>/dev/null; then log "controller STOPPED"; else log "controller already stopped"; fi
    relay_coord stop FAULT   # stop publishing relay allowlists — we're unfit
    api_remount FAULT    # unmount the control plane — we're unfit
    ;;
  *)
    log "unknown/empty STATE '$STATE' (noop)"
    ;;
esac
