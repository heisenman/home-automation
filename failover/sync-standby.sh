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

# 1. Plain config/secret files the standby needs to RUN + ACTUATE after takeover. Driven by the shared
#    failover/dictator-files.manifest (DRY with cluster-doctor's presence assertion) so the set can't
#    drift. We pull every row marked `sync` — notably control_secrets.yaml + node_secrets.enc, whose
#    absence at the 2026-06-24 cutover left the controller unable to actuate (unknown-device). The Midea
#    token (rotates ~18h) is in that set and is THE freshness-critical one. `.master_pass` is deliberately
#    NOT here (disposition=preposition — root trust, never sent over the wire; cluster-doctor asserts it).
#    DBs (control.db/mesh.db) get a consistent sqlite snapshot below, not a raw scp.
MANIFEST="$HERE/dictator-files.manifest"
if [ -f "$MANIFEST" ]; then
  while read -r rel; do
    [ -n "$rel" ] || continue
    if SCP "visko@$PEER_HOST:$REMOTE_REPO/$rel" "$REPO/$rel" 2>/dev/null; then
      chmod 600 "$REPO/$rel" 2>/dev/null || true; log "synced $rel"
    else log "WARN: $rel sync failed (peer down or absent on primary)"; fi
  done < <(awk -F'|' '/^[[:space:]]*#/ || NF<3 {next}
                      {gsub(/^[[:space:]]+|[[:space:]]+$/,"",$1); gsub(/^[[:space:]]+|[[:space:]]+$/,"",$3)
                       if ($3=="sync") print $1}' "$MANIFEST")
else
  log "WARN: $MANIFEST missing — falling back to midea-device.env + control.yaml only"
  SCP "visko@$PEER_HOST:$REMOTE_REPO/instance/midea-device.env" "$REPO/instance/midea-device.env" 2>/dev/null \
    && { chmod 600 "$REPO/instance/midea-device.env" 2>/dev/null || true; log "synced midea-device.env"; } || log "WARN: midea-device.env sync failed"
  SCP "visko@$PEER_HOST:$REMOTE_REPO/instance/control.yaml" "$REPO/instance/control.yaml" 2>/dev/null && log "synced control.yaml" || log "control.yaml not synced"
fi

# Consistent sqlite snapshot of a remote DB -> local (falls back to raw copy if sqlite3 is absent).
sync_db(){   # $1 = relative path under instance/db
  local name="$1" rel="instance/db/$1"
  RSH "test -f $REMOTE_REPO/$rel" || return 0
  if RSH "command -v sqlite3 >/dev/null && sqlite3 $REMOTE_REPO/$rel \".backup /tmp/$name.snap\"" 2>/dev/null; then
    SCP "visko@$PEER_HOST:/tmp/$name.snap" "$REPO/$rel" 2>/dev/null && log "synced $name (consistent snapshot)" || log "WARN: $name snapshot copy failed"
    RSH "rm -f /tmp/$name.snap" 2>/dev/null || true
  else
    SCP "visko@$PEER_HOST:$REMOTE_REPO/$rel" "$REPO/$rel" 2>/dev/null && log "synced $name (raw copy; sqlite3 absent)" || log "WARN: $name raw copy failed"
  fi
}

# 3. Live control policy/state (control.db) + the mesh reach/assignment graph (mesh.db, ADR-0015 §3) —
#    seed the standby so it inherits coverage knowledge; it then RECOMPUTES assignments from its own
#    reach on promotion (notify.sh master restarts the mapper).
sync_db control.db
sync_db mesh.db
log "sync complete"
