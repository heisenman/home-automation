#!/usr/bin/env bash
# keepalived notify dispatcher — binds ha-controller to VRRP state so exactly ONE box (the VIP
# holder / MASTER) ever runs the controller.
#   keepalived calls: notify.sh <INSTANCE|GROUP> <name> <STATE> [priority]   (STATE in $3)
#   manual test:      notify.sh MASTER     (STATE in $1)
# MASTER  -> FENCE the peer (stop its controller), verify secrets, start OUR controller.
# BACKUP  -> stop OUR controller (step down — this is the Core-Rule auto-demote path under preempt).
# FAULT   -> stop OUR controller (we're unfit).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
[ -f "$REPO/instance/cluster.env" ] && . "$REPO/instance/cluster.env"
: "${CONTROLLER_UNIT:=ha-controller}"; : "${PEER_HOST:=}"; : "${CLUSTER_KEY:=$HOME/.ssh/id_cluster}"
: "${VIP:=192.168.0.200}"; : "${BACKUP_GRACE:=4}"   # see BACKUP branch — startup-transient suppression
LOG=/var/log/ha-failover.log
STATE="${3:-${1:-}}"

log(){ printf '%s notify[%s] %s\n' "$(date -Is)" "$STATE" "$*" | tee -a "$LOG" 2>/dev/null || true; }
peer_ssh(){ ssh -i "$CLUSTER_KEY" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "visko@$PEER_HOST" "$@"; }

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
    ;;
  FAULT)
    # Health check failed: we are genuinely unfit. Stop immediately — no grace (don't keep actuating while broken).
    log "becoming FAULT -> stop controller immediately (unfit)"
    if sudo systemctl stop "$CONTROLLER_UNIT" 2>/dev/null; then log "controller STOPPED"; else log "controller already stopped"; fi
    ;;
  *)
    log "unknown/empty STATE '$STATE' (noop)"
    ;;
esac
