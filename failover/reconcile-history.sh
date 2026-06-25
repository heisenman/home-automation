#!/usr/bin/env bash
# ADR-0016 — sensor-history reconciliation across a dictator failover.
#
# Bidirectional, IDEMPOTENT windowed merge of hot.db `readings` between the two cluster boxes over the
# id_cluster SSH back-channel (the same mechanism sync-standby uses — never the device bus, never git).
# Idempotency rides the writer's UNIQUE(device_id, ts, metric) index + INSERT OR IGNORE, so an overlapping
# re-merge is a no-op and a missed run self-corrects on the next one. WINDOW = the compactor's cutoff
# (yesterday 00:00 UTC) so it self-tracks exactly the hot tier (where live divergence lives); never a
# hand-tuned constant.
#
# Modes:
#   reconcile-history.sh --once     one bidirectional pass (notify.sh transition hook + manual/dry-run)
#   reconcile-history.sh --loop     proactive daemon (VIP-gated): every RECONCILE_INTERVAL_S push+pull so the
#                                   standby stays within ~one interval of current — cuts loss on a sudden
#                                   primary death (incl. disk loss). Plus a SHADOW tuner that LOGS a proposed
#                                   adaptive interval each cycle but does NOT apply it (15 min stays fixed
#                                   until a week of shadow data is reviewed → then flip RECONCILE_MODE=active).
#   reconcile-history.sh --export <snap> <cutoff>   (remote primitive) dump local readings>=cutoff to a snapshot
#   reconcile-history.sh --merge  <snap>            (remote primitive) INSERT OR IGNORE a snapshot into local hot.db
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
[ -f "$REPO/instance/cluster.env" ] && . "$REPO/instance/cluster.env"
[ -f "$REPO/instance/reconcile-tuning.env" ] && . "$REPO/instance/reconcile-tuning.env"
: "${PEER_HOST:=}"; : "${CLUSTER_KEY:=$HOME/.ssh/id_cluster}"; : "${REMOTE_REPO:=/home/visko/home_automation}"
: "${VIP:=192.168.0.200}"; : "${HOT_DB:=instance/db/hot.db}"
# Tuned-state (SEEDED). Shadow tuner LOGS `proposed` only; the ACTIVE cadence stays RECONCILE_INTERVAL_S
# until RECONCILE_MODE=active is flipped after the week-long shadow review.
: "${RECONCILE_INTERVAL_S:=900}"     # ACTIVE proactive cadence = fixed 15 min
: "${RECONCILE_DUTY_PCT:=8}"         # target reconcile duty cycle D/interval (%) -> interval floor = D*100/PCT
: "${RECONCILE_I_MIN_S:=120}"        # lower clamp (no BLE/scp thrash, never < reconcile duration)
: "${RECONCILE_I_MAX_S:=900}"        # upper clamp = LOSS BUDGET (the one human input) ∧ ≤ hot WINDOW
: "${RECONCILE_MODE:=shadow}"        # shadow = compute+log proposed (active fixed); active = drive from proposed
RLOG=/var/log/ha-reconcile.log
SHADOWLOG=/var/log/ha-reconcile-tuning.log
case "$HOT_DB" in /*) DB="$HOT_DB";; *) DB="$REPO/$HOT_DB";; esac   # absolute or repo-relative

log(){ printf '%s reconcile %s\n' "$(date -Is)" "$*" | tee -a "$RLOG" 2>/dev/null || true; }
RSH(){ ssh -i "$CLUSTER_KEY" -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new "visko@$PEER_HOST" "$@"; }
SCP(){ scp -i "$CLUSTER_KEY" -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new "$@"; }
have_sqlite(){ command -v sqlite3 >/dev/null; }

# the compactor horizon — everything with ts >= this is still in hot.db (matches compactor._cutoff_ts()).
cutoff(){ date -u -d "yesterday 00:00:00" +%Y-%m-%dT00:00:00Z 2>/dev/null || date -u -v-1d +%Y-%m-%dT00:00:00Z; }
rows(){ sqlite3 "$1" "SELECT COUNT(*) FROM readings;" 2>/dev/null || echo 0; }
_COLS="ts,device_id,device_type,area,transport,metric,value,unit,schema_v,authoritative"

# --- remote primitives (run on either box) -----------------------------------------------------------
do_export(){ # $1=snap path  $2=cutoff   -> throwaway snapshot of local readings >= cutoff
  rm -f "$1"
  sqlite3 "$DB" "ATTACH '$1' AS s; CREATE TABLE s.readings AS SELECT * FROM readings WHERE ts >= '$2';"
  rows "$1"
}
do_merge(){ # $1=snap path  -> INSERT OR IGNORE into local hot.db; echoes rows actually added
  local before after
  before=$(rows "$DB")
  sqlite3 "$DB" "ATTACH '$1' AS s; INSERT OR IGNORE INTO readings($_COLS) SELECT $_COLS FROM s.readings;"
  after=$(rows "$DB")
  echo $(( after - before ))
}

# --- one bidirectional pass --------------------------------------------------------------------------
reconcile_once(){
  [ -n "$PEER_HOST" ] || { log "no PEER_HOST — skip"; return 0; }
  have_sqlite || { log "sqlite3 absent — skip"; return 0; }
  [ -f "$DB" ] || { log "no local hot.db ($DB) — skip"; return 0; }
  local co; co=$(cutoff)
  # PULL: backfill rows the peer has that we missed (the other reign's data).
  if RSH "cd $REMOTE_REPO && bash failover/reconcile-history.sh --export /tmp/ha-recon-peer.snap '$co'" >/dev/null 2>&1 \
     && SCP "visko@$PEER_HOST:/tmp/ha-recon-peer.snap" /tmp/ha-recon-peer.snap >/dev/null 2>&1; then
    local got; got=$(do_merge /tmp/ha-recon-peer.snap); rm -f /tmp/ha-recon-peer.snap
    log "pull: merged $got row(s) from peer (window >= $co)"
  else
    log "pull: peer unreachable/export-failed (ok — self-corrects next pass)"
  fi
  # PUSH: hand the peer the rows it's missing (our reign's data) — keeps the standby current.
  local n; n=$(do_export /tmp/ha-recon-local.snap "$co")
  if SCP /tmp/ha-recon-local.snap "visko@$PEER_HOST:/tmp/ha-recon-local.snap" >/dev/null 2>&1; then
    local pushed; pushed=$(RSH "cd $REMOTE_REPO && bash failover/reconcile-history.sh --merge /tmp/ha-recon-local.snap" 2>/dev/null || echo "?")
    log "push: sent $n row window -> peer merged $pushed new"
  else
    log "push: peer unreachable (ok — self-corrects next pass)"
  fi
  rm -f /tmp/ha-recon-local.snap
}

# --- SHADOW tuner: compute a bounded proposed interval, LOG it, never apply (15 min stays fixed) ------
shadow_tune(){ # $1=last reconcile duration D (s)
  local d="$1" floor proposed rate
  floor=$(( d * 100 / RECONCILE_DUTY_PCT ))          # D/δ — interval must be ≥ this (duty-cycle cap)
  proposed=$floor
  [ "$proposed" -lt "$RECONCILE_I_MIN_S" ] && proposed=$RECONCILE_I_MIN_S
  [ "$proposed" -gt "$RECONCILE_I_MAX_S" ] && proposed=$RECONCILE_I_MAX_S   # ≤ loss budget ∧ hot window
  rate=$(sqlite3 "$DB" "SELECT COUNT(*) FROM readings WHERE ts >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-${RECONCILE_INTERVAL_S} seconds');" 2>/dev/null || echo 0)
  printf '%s D=%ss rows_recent=%s active=%ss proposed=%ss mode=%s\n' \
    "$(date -Is)" "$d" "$rate" "$RECONCILE_INTERVAL_S" "$proposed" "$RECONCILE_MODE" | tee -a "$SHADOWLOG" 2>/dev/null || true
  # SAFETY red flag: reconcile cost approaching the cap means the cheap path can't keep pace.
  if [ "$d" -gt $(( RECONCILE_I_MAX_S / 2 )) ]; then
    log "WARN: reconcile D=${d}s > half of I_max (${RECONCILE_I_MAX_S}s) — cheap path under strain"
  fi
}

case "${1:---once}" in
  --export) do_export "$2" "$3" ;;
  --merge)  do_merge "$2" ;;
  --once)   reconcile_once ;;
  --loop)
    log "proactive reconcile loop up: active=${RECONCILE_INTERVAL_S}s mode=$RECONCILE_MODE (VIP-gated $VIP)"
    while true; do
      if ip -o addr show 2>/dev/null | grep -qw "$VIP"; then       # only the dictator pushes live data
        t0=$(date +%s); reconcile_once; t1=$(date +%s)
        shadow_tune $(( t1 - t0 ))
      fi
      sleep "$RECONCILE_INTERVAL_S"                                # ACTIVE cadence (fixed until promoted)
    done ;;
  *) echo "usage: $0 [--once|--loop|--export <snap> <cutoff>|--merge <snap>]" >&2; exit 2 ;;
esac
