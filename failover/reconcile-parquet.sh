#!/usr/bin/env bash
# ADR-0018 / ADR-0016 deep-reconcile — ROW-LEVEL parquet archive reconciliation across cluster boxes.
#
# The cold-tier sibling of reconcile-history.sh. Where that merges the HOT tier (today's sqlite, the
# 15-min divergence window), this merges the PARQUET ARCHIVE (compacted history, months deep) so a box
# elevated to record-keeping status holds the FULL timeline — not just the slice it has ingested itself.
#
# Why this exists: the 2026-06-25 finding — 210 was elevated to dictator with only ~1.5 d of archive
# (the cutover seeded hot+config but never the parquet archive that lived on .245). ADR-0016 deferred the
# parquet deep-reconcile; ADR-0018 promotes it to load-bearing and makes archive-completeness a HARD gate
# for dictator-of-record eligibility.
#
# Mechanism (Hugh's call: row-level merge, NOT file rsync). Per monthly partition, the merged result is the
# DISTINCT union of both boxes' rows keyed on the writer's identity (device_id, ts, metric) — the SAME
# idempotency contract as hot-tier ingestion (ADR-0007). So an overlapping re-merge is a no-op, a missed
# run self-corrects next pass, and a partial-month divergence heals cleanly (vs a coarse file rsync, which
# can't merge two boxes that each hold different rows for the same month). Each partition is rebuilt to a
# temp file and atomically mv'd into place (the API opens parquet fresh per query).
#
# Transport: id_cluster SSH back-channel ONLY (same rule as sync-standby/reconcile-history — never the
# device bus, never git).
#
# Modes:
#   reconcile-parquet.sh --once            one bidirectional pass over ALL partitions (seed + converge)
#   reconcile-parquet.sh --loop            VIP-gated periodic deep-reconcile (slow cadence; parquet only
#                                          diverges when a swap straddles a daily compaction boundary)
#   reconcile-parquet.sh --list            (remote primitive) list local partition relpaths
#   reconcile-parquet.sh --merge <rel> <f> (remote primitive) row-merge parquet file <f> into local <rel>
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
[ -f "$REPO/instance/cluster.env" ] && . "$REPO/instance/cluster.env"
[ -f "$REPO/instance/reconcile-tuning.env" ] && . "$REPO/instance/reconcile-tuning.env"
: "${PEER_HOST:=}"; : "${CLUSTER_KEY:=$HOME/.ssh/id_cluster}"; : "${REMOTE_REPO:=/home/visko/home_automation}"
: "${VIP:=192.168.0.200}"; : "${PARQUET_DIR:=instance/db/parquet}"
: "${PYBIN:=$REPO/venv/bin/python}"; [ -x "$PYBIN" ] || PYBIN="$(command -v python3 || echo python3)"
: "${RECONCILE_PARQUET_INTERVAL_S:=21600}"   # 6 h — parquet changes ~daily (compaction); no need to thrash
: "${RECONCILE_LOG:=$REPO/instance/ha-reconcile.log}"
RLOG="$RECONCILE_LOG"
case "$PARQUET_DIR" in /*) PQ="$PARQUET_DIR";; *) PQ="$REPO/$PARQUET_DIR";; esac

log(){ printf '%s reconcile-parquet %s\n' "$(date -Is)" "$*" | tee -a "$RLOG" 2>/dev/null || true; }
# -n (stdin from /dev/null) is REQUIRED: RSH is called inside the partition while-read loop below, and a
# bare ssh would consume the loop's stdin and process only the first partition (the 2026-06-25 bug).
RSH(){ ssh -n -i "$CLUSTER_KEY" -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new "visko@$PEER_HOST" "$@"; }
SCP(){ scp -i "$CLUSTER_KEY" -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new "$@"; }

# list local partition files as relpaths under the parquet root (e.g. year=2026/month=03/2026-03.parquet),
# excluding the stray year=0 partition that a bad compaction can leave behind.
list_local(){ [ -d "$PQ" ] || return 0; ( cd "$PQ" && find . -name '*.parquet' ! -path './year=0/*' -printf '%P\n' 2>/dev/null | sort ); }

# Row-level merge: rebuild local <rel> as DISTINCT-union(local, peerfile) keyed (device_id, ts, metric).
# Creates the partition dir if this is a seed (local had nothing). Atomic mv. Echoes the resulting rowcount.
do_merge(){ # $1=relpath  $2=peer parquet file (may be empty/absent)
  local rel="$1" peerf="${2:-}" out="$PQ/$1" dir; dir="$(dirname "$out")"
  mkdir -p "$dir"
  "$PYBIN" - "$out" "$out" "$peerf" <<'PY'
import os, sys
out = sys.argv[1]
inputs = [p for p in sys.argv[2:] if p and os.path.exists(p) and os.path.getsize(p) > 0]
if not inputs:
    print(0); sys.exit(0)
import duckdb
con = duckdb.connect()
tmp = out + ".tmp"
con.execute(f"""
  COPY (
    SELECT * EXCLUDE (rn) FROM (
      SELECT *, row_number() OVER (
        PARTITION BY device_id, ts, metric ORDER BY value DESC NULLS LAST
      ) AS rn
      FROM read_parquet({inputs!r}, union_by_name=true)
    ) WHERE rn = 1
    ORDER BY device_id, metric, ts
  ) TO '{tmp}' (FORMAT parquet, COMPRESSION zstd, COMPRESSION_LEVEL 6, ROW_GROUP_SIZE 100000);
""")  # match server/storage/compactor.py (zstd/6/100k) + sort for column locality so a merge doesn't bloat the archive
os.replace(tmp, out)                       # atomic within the same dir
print(con.execute(f"SELECT COUNT(*) FROM read_parquet(['{out}'])").fetchone()[0])
PY
}

# --- one bidirectional pass over the union of both boxes' partitions ----------------------------------
reconcile_once(){
  [ -n "$PEER_HOST" ] || { log "no PEER_HOST — skip"; return 0; }
  [ -x "$PYBIN" ] || { command -v "$PYBIN" >/dev/null || { log "no python ($PYBIN) — skip"; return 0; }; }
  "$PYBIN" -c 'import duckdb' 2>/dev/null || { log "duckdb absent in $PYBIN — skip (install in the venv)"; return 0; }
  mkdir -p "$PQ"
  local locals peers union rel pf added pushed
  locals="$(list_local)"
  peers="$(RSH "cd $REMOTE_REPO && bash failover/reconcile-parquet.sh --list" 2>/dev/null || true)"
  union="$(printf '%s\n%s\n' "$locals" "$peers" | sed '/^$/d' | sort -u)"
  [ -n "$union" ] || { log "no partitions on either box — nothing to reconcile"; return 0; }
  while IFS= read -r rel <&3; do
    [ -n "$rel" ] || continue
    # PULL: bring the peer's copy of this partition over and row-merge it into ours.
    if printf '%s\n' "$peers" | grep -qxF "$rel"; then
      pf="/tmp/pq-peer-$$.parquet"
      if SCP "visko@$PEER_HOST:$REMOTE_REPO/$PARQUET_DIR/$rel" "$pf" >/dev/null 2>&1; then
        added="$(do_merge "$rel" "$pf")"; rm -f "$pf"
        log "pull: $rel -> local now $added row(s)"
      else
        log "pull: $rel scp failed (ok — self-corrects next pass)"
      fi
    fi
    # PUSH: hand the peer our (now-merged) partition so it converges to the same union.
    if [ -f "$PQ/$rel" ]; then
      if SCP "$PQ/$rel" "visko@$PEER_HOST:/tmp/pq-local-$$.parquet" >/dev/null 2>&1; then
        pushed="$(RSH "cd $REMOTE_REPO && bash failover/reconcile-parquet.sh --merge '$rel' /tmp/pq-local-$$.parquet; rm -f /tmp/pq-local-$$.parquet" 2>/dev/null || echo '?')"
        log "push: $rel -> peer now $pushed row(s)"
      else
        log "push: $rel scp->peer failed (ok — self-corrects next pass)"
      fi
    fi
  done 3<<<"$union"        # read on FD 3 so ssh/scp in the body can't steal the loop's stdin
  log "parquet reconcile pass complete ($(printf '%s\n' "$union" | grep -c . ) partition(s))"
  # ADR-0004 manifest: the merges above rewrote/added partitions, so manifest.json is now stale on BOTH
  # boxes. Rebuild it in the SAME pass — otherwise ha-verify-hashes false-alarms MISMATCH/MISSING on a
  # correct archive (the seam found 2026-06-26, post-ADR-0018-seed). Non-fatal: a missing tool or an
  # out-of-date peer just self-corrects on the next pass. Read-only verify is asserted in cluster-doctor.
  local REBUILD="tools/rebuild_parquet_manifest.py"
  if [ -f "$REPO/$REBUILD" ]; then
    "$PYBIN" "$REPO/$REBUILD" --parquet-dir "$PQ" >/dev/null 2>&1 \
      && log "manifest rebuilt (local)" || log "manifest rebuild (local) failed — self-corrects next pass"
  fi
  RSH "cd '$REMOTE_REPO' && [ -f '$REBUILD' ] && venv/bin/python3 '$REBUILD' --parquet-dir '$PARQUET_DIR' >/dev/null 2>&1" \
    && log "manifest rebuilt (peer $PEER_HOST)" || log "manifest rebuild (peer) skipped/failed — self-corrects next pass"
}

case "${1:---once}" in
  --list)   list_local ;;
  --merge)  do_merge "$2" "${3:-}" ;;
  --once)   reconcile_once ;;
  --loop)
    log "deep-reconcile loop up: interval=${RECONCILE_PARQUET_INTERVAL_S}s (VIP-gated $VIP)"
    while true; do
      if ip -o addr show 2>/dev/null | grep -qw "$VIP"; then reconcile_once; fi
      sleep "$RECONCILE_PARQUET_INTERVAL_S"
    done ;;
  *) echo "usage: $0 [--once|--loop|--list|--merge <relpath> <peerfile>]" >&2; exit 2 ;;
esac
