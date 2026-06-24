#!/usr/bin/env bash
# Standby-only state sync: pull freshness-critical state from the PRIMARY so the standby can take
# over faithfully. SSH/scp ONLY (these are secrets — never via git). Runs from a systemd timer.
# Skips entirely if WE are currently acting as dictator (then we are the source of truth, not the sink).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
[ -f "$REPO/instance/cluster.env" ] && . "$REPO/instance/cluster.env"
: "${ROLE:=standby}"; : "${PEER_HOST:=}"; : "${CLUSTER_KEY:=$HOME/.ssh/id_cluster}"
: "${REMOTE_REPO:=/home/visko/home_automation}"; : "${CONTROLLER_UNIT:=ha-controller}"
LOG=/var/log/ha-failover.log
log(){ printf '%s sync-standby %s\n' "$(date -Is)" "$*" | tee -a "$LOG" 2>/dev/null || true; }
RSH(){ ssh -i "$CLUSTER_KEY" -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new "visko@$PEER_HOST" "$@"; }
SCP(){ scp -i "$CLUSTER_KEY" -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new "$@"; }

[ "$ROLE" = standby ] || { log "ROLE=$ROLE — not a standby, nothing to sync"; exit 0; }
if systemctl is-active --quiet "$CONTROLLER_UNIT"; then log "WE are acting dictator — skip (don't overwrite our own live state)"; exit 0; fi
[ -n "$PEER_HOST" ] || { log "no PEER_HOST set"; exit 1; }
mkdir -p "$REPO/instance/db"

# 1. Midea token (rotates ~18h) — THE critical freshness item for being able to actuate after takeover.
if SCP "visko@$PEER_HOST:$REMOTE_REPO/instance/midea-device.env" "$REPO/instance/midea-device.env" 2>/dev/null; then
  chmod 600 "$REPO/instance/midea-device.env" 2>/dev/null || true; log "synced midea-device.env"
else log "WARN: midea-device.env sync failed (peer down?)"; fi

# 2. Declarative control config (if present on primary).
SCP "visko@$PEER_HOST:$REMOTE_REPO/instance/control.yaml" "$REPO/instance/control.yaml" 2>/dev/null && log "synced control.yaml" || log "control.yaml not synced (may not exist)"

# 3. Live control policy/state (control.db). Prefer a consistent sqlite snapshot; fall back to raw copy.
if RSH "test -f $REMOTE_REPO/instance/db/control.db"; then
  if RSH "command -v sqlite3 >/dev/null && sqlite3 $REMOTE_REPO/instance/db/control.db \".backup /tmp/control.snap.db\"" 2>/dev/null; then
    SCP "visko@$PEER_HOST:/tmp/control.snap.db" "$REPO/instance/db/control.db" 2>/dev/null && log "synced control.db (consistent snapshot)" || log "WARN: control.db snapshot copy failed"
    RSH "rm -f /tmp/control.snap.db" 2>/dev/null || true
  else
    SCP "visko@$PEER_HOST:$REMOTE_REPO/instance/db/control.db" "$REPO/instance/db/control.db" 2>/dev/null && log "synced control.db (raw copy; sqlite3 absent)" || log "WARN: control.db raw copy failed"
  fi
fi
log "sync complete"
