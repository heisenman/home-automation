#!/usr/bin/env bash
# PRIMARY-SUPREMACY watchdog (standby box only) — the app-level guarantee of Hugh's Core Rule,
# redundant with keepalived preempt: if THIS standby is currently running the controller AND the
# primary has been healthily back for >= DEBOUNCE seconds, demote ourselves (stop controller).
# Runs as a long-lived systemd service on the standby. Does NOT promote (that's keepalived's job);
# it only ever YIELDS — a standby can never permanently hold control while the primary is healthy.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
[ -f "$REPO/instance/cluster.env" ] && . "$REPO/instance/cluster.env"
: "${ROLE:=standby}"; : "${CONTROLLER_UNIT:=ha-controller}"; : "${PEER_HOST:=}"
: "${CLUSTER_KEY:=$HOME/.ssh/id_cluster}"; : "${DEBOUNCE:=30}"; : "${POLL:=5}"
LOG=/var/log/ha-failover.log
log(){ printf '%s primary-watch %s\n' "$(date -Is)" "$*" | tee -a "$LOG" 2>/dev/null || true; }

[ "$ROLE" = standby ] || { log "ROLE=$ROLE (not standby) — primary-watch is a no-op here"; exec sleep infinity; }

primary_healthy(){
  # Primary is "healthy" if its controller is active (it has reclaimed dictatorship).
  ssh -i "$CLUSTER_KEY" -o BatchMode=yes -o ConnectTimeout=4 -o StrictHostKeyChecking=accept-new \
      "visko@$PEER_HOST" "systemctl is-active $CONTROLLER_UNIT" 2>/dev/null | grep -q '^active$'
}

log "started (peer=$PEER_HOST, debounce=${DEBOUNCE}s, poll=${POLL}s)"
healthy_since=0
while true; do
  if systemctl is-active --quiet "$CONTROLLER_UNIT"; then
    # We (standby) are acting as dictator. Is the primary back?
    if primary_healthy; then
      now=$(date +%s); [ "$healthy_since" -eq 0 ] && { healthy_since=$now; log "primary controller is active again — starting ${DEBOUNCE}s yield debounce"; }
      if [ $(( now - healthy_since )) -ge "$DEBOUNCE" ]; then
        log "primary healthy >= ${DEBOUNCE}s -> AUTO-DEMOTE (primary supremacy): stopping our controller"
        sudo systemctl stop "$CONTROLLER_UNIT" 2>/dev/null && log "yielded to primary; back to standby" || log "stop failed (already stopped?)"
        healthy_since=0
      fi
    else
      [ "$healthy_since" -ne 0 ] && log "primary went unhealthy again — cancel yield"
      healthy_since=0
    fi
  else
    healthy_since=0   # we're not acting as dictator; nothing to yield
  fi
  sleep "$POLL"
done
