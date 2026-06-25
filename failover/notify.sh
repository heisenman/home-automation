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
: "${CONTROLLER_UNIT:=ha-controller}"; : "${RELAY_COORD_UNIT:=ha-relay-coordinator}"; : "${PEER_HOST:=}"; : "${CLUSTER_KEY:=$HOME/.ssh/id_cluster}"
: "${VIP:=192.168.0.200}"; : "${BACKUP_GRACE:=4}"   # see BACKUP branch — startup-transient suppression
LOG=/var/log/ha-failover.log
STATE="${3:-${1:-}}"

log(){ printf '%s notify[%s] %s\n' "$(date -Is)" "$STATE" "$*" | tee -a "$LOG" 2>/dev/null || true; }
peer_ssh(){ ssh -i "$CLUSTER_KEY" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "visko@$PEER_HOST" "$@"; }
# ADR-0015 #9: restart ha-api so its control plane re-evaluates the VIP gate (mounts on the dictator,
# stays read-only on the standby). Best-effort, active-only, never blocks the VRRP transition.
api_remount(){ systemctl is-active --quiet ha-api && { sudo systemctl restart ha-api 2>/dev/null && log "ha-api restarted ($1) — control plane re-evaluates VIP gate" || log "ha-api restart skipped/failed (non-fatal)"; }; return 0; }
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

case "$STATE" in
  MASTER)
    log "becoming MASTER -> fence peer, then start controller"
    # FENCE first: best-effort stop the peer's controller (covers alive-but-not-master / split-brain heal).
    # A failure here means the peer is down/unreachable — which is fine, a dead peer isn't controlling.
    if [ -n "$PEER_HOST" ]; then
      if peer_ssh "sudo systemctl stop $CONTROLLER_UNIT" 2>/dev/null; then log "fenced peer $PEER_HOST (controller stopped)"; else log "peer fence failed/unreachable (ok if peer is down)"; fi
    fi
    # Never start a controller that can't build its issuer.
    if [ ! -f "$REPO/instance/.master_pass" ]; then log "ABORT: missing instance/.master_pass — NOT starting controller"; exit 1; fi
    if sudo systemctl start "$CONTROLLER_UNIT"; then log "controller STARTED — this box is now the dictator"; else log "ERROR: failed to start $CONTROLLER_UNIT"; exit 1; fi
    # ADR-0015 §3: recompute relay coverage from THIS box's own reach (the new dictator hears a different
    # set). Replicate-to-seed (sync-standby brought mesh.db); recompute-to-be-correct (restart the mapper).
    # Best-effort, active-only, never blocks the takeover.
    if systemctl is-active --quiet ha-edge-mapper; then
      sudo systemctl restart ha-edge-mapper 2>/dev/null && log "edge-mapper restarted — recomputing coverage from local reach" || log "edge-mapper restart skipped/failed (non-fatal)"
    fi
    relay_coord start MASTER   # this box now signs+publishes relay allowlists (re-eval after coverage recompute)
    api_remount MASTER   # mount the control plane now that this node holds the VIP
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
