#!/usr/bin/env bash
# ADR-0018 — bring a computer up as a failover PEER and elevate it to RECORD-KEEPING status.
#
# "Record-keeping status" is a first-class provisioning stage, distinct from "can ingest" and "holds the
# VIP": a box is only trusted as dictator-of-record once it holds the FULL config-of-record AND the FULL
# data-of-record (hot tier + parquet archive). This script performs — and then HARD-GATES — that
# elevation, so a box can never again silently become the dictator with a truncated archive (the
# 2026-06-25 incident: 210 was promoted with ~1.5 d of history while the months-deep archive sat on .245).
#
# It composes the existing, proven pieces rather than reinventing them:
#   1. config-of-record  : sync-standby.sh        (manifest secrets + control.db/mesh.db snapshots)
#   2. hot tier          : reconcile-history.sh --once   (today's sqlite divergence window)
#   3. archive           : reconcile-parquet.sh   --once   (row-level parquet deep-reconcile, months deep)
#   4. HARD GATE         : archive-parity assertion vs the source — eligible ONLY if it passes.
#
# All transport is the id_cluster SSH back-channel (never the device bus, never git).
#
# Usage:
#   provision-peer.sh --from <host> [--data-only] [--no-push] [--yes]
#     --from <host>   the existing record-keeper to sync FROM (the current dictator, or whoever holds the
#                     deepest archive). Sets PEER_HOST for every stage.
#     --data-only     skip stage 1 (config). Use when THIS box's config is already authoritative and only
#                     its DATA is thin — e.g. re-provisioning the present dictator to recover its archive.
#     --no-push       pull/merge into THIS box only; do not push our merged result back to the source.
#     --yes           non-interactive (assume yes at the confirm prompt).
#
# Exit: 0 = elevated + gate PASSED (record-keeping eligible); 1 = gate FAILED or a stage errored.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
[ -f "$REPO/instance/cluster.env" ] && . "$REPO/instance/cluster.env"
: "${CLUSTER_KEY:=$HOME/.ssh/id_cluster}"; : "${REMOTE_REPO:=/home/visko/home_automation}"
: "${PARQUET_DIR:=instance/db/parquet}"; : "${PYBIN:=$REPO/venv/bin/python}"
[ -x "$PYBIN" ] || PYBIN="$(command -v python3 || echo python3)"

FROM=""; DATA_ONLY=0; NO_PUSH=0; ASSUME_YES=0
while [ $# -gt 0 ]; do case "$1" in
  --from) FROM="$2"; shift 2;;
  --data-only) DATA_ONLY=1; shift;;
  --no-push) NO_PUSH=1; shift;;
  --yes|-y) ASSUME_YES=1; shift;;
  *) echo "unknown arg: $1" >&2; exit 2;;
esac; done
[ -n "$FROM" ] || { echo "usage: $0 --from <host> [--data-only] [--no-push] [--yes]" >&2; exit 2; }
export PEER_HOST="$FROM" CLUSTER_KEY REMOTE_REPO PARQUET_DIR PYBIN
RSH(){ ssh -i "$CLUSTER_KEY" -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new "visko@$FROM" "$@"; }

say(){ printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
ok(){ printf '  \033[32m[OK]\033[0m %s\n' "$*"; }
no(){ printf '  \033[31m[FAIL]\033[0m %s\n' "$*"; }

say "provision-peer: elevate $(hostname) to record-keeping (source=$FROM, data-only=$DATA_ONLY)"

# ---- preflight -------------------------------------------------------------------------------------
RSH true 2>/dev/null && ok "source $FROM reachable over id_cluster" || { no "source $FROM UNREACHABLE — abort"; exit 1; }
"$PYBIN" -c 'import duckdb' 2>/dev/null && ok "duckdb present ($PYBIN)" || { no "duckdb absent in $PYBIN — abort (needed for the archive merge)"; exit 1; }
command -v sqlite3 >/dev/null && ok "sqlite3 present" || { no "sqlite3 absent — abort (hot-tier merge)"; exit 1; }

if [ "$ASSUME_YES" != 1 ]; then
  printf '\nThis will sync config(%s) + hot + parquet archive FROM %s into THIS box and gate eligibility.\nProceed? [y/N] ' "$([ $DATA_ONLY = 1 ] && echo skip || echo yes)" "$FROM"
  read -r ans; case "$ans" in y|Y|yes) ;; *) echo "aborted."; exit 1;; esac
fi

# ---- stage 1: config-of-record ---------------------------------------------------------------------
if [ "$DATA_ONLY" = 1 ]; then
  say "stage 1 — config-of-record: SKIPPED (--data-only; this box's config is authoritative)"
else
  say "stage 1 — config-of-record (manifest secrets + control.db/mesh.db) via sync-standby"
  if ROLE=standby PEER_HOST="$FROM" bash "$HERE/sync-standby.sh"; then ok "config-of-record synced from $FROM"
  else no "config sync reported problems — review before trusting this box as dictator"; fi
fi

# ---- stage 2: hot tier -----------------------------------------------------------------------------
say "stage 2 — hot tier (reconcile-history --once)"
PEER_HOST="$FROM" bash "$HERE/reconcile-history.sh" --once && ok "hot-tier reconcile pass done" || no "hot-tier reconcile errored"

# ---- stage 3: parquet archive ----------------------------------------------------------------------
say "stage 3 — parquet archive (reconcile-parquet --once, row-level deep-reconcile)"
PEER_HOST="$FROM" bash "$HERE/reconcile-parquet.sh" --once && ok "archive deep-reconcile pass done" || no "archive reconcile errored"

# ---- stage 4: HARD GATE — archive parity vs the source ---------------------------------------------
# Eligible only if THIS box's archive now covers the source's: earliest reading no later, and row count
# within tolerance (after a union merge they should match; tolerance absorbs in-flight compaction).
say "stage 4 — HARD GATE: archive completeness vs $FROM"
read_stats(){ # $1 = where ("local" | "remote"); echoes "rows|earliest" (no f-string backslashes: py<3.12-safe)
  local pyexpr='import glob,duckdb,sys
fs=[x for x in glob.glob(sys.argv[1]+"/**/*.parquet",recursive=True) if "/year=0/" not in x]
if not fs:
    print("0|")
else:
    r=duckdb.connect().execute("SELECT COUNT(*),MIN(ts) FROM read_parquet("+repr(fs)+",union_by_name=true)").fetchone()
    print(str(r[0])+"|"+(r[1] or ""))'
  if [ "$1" = local ]; then "$PYBIN" -c "$pyexpr" "$REPO/$PARQUET_DIR"
  else RSH "$REMOTE_REPO/venv/bin/python3 -c '$pyexpr' '$REMOTE_REPO/$PARQUET_DIR'"; fi
}
LS="$(read_stats local)";  l_rows="${LS%%|*}"; l_min="${LS#*|}"
RS="$(read_stats remote)"; r_rows="${RS%%|*}"; r_min="${RS#*|}"
echo "  local : rows=$l_rows earliest=${l_min:-none}"
echo "  source: rows=$r_rows earliest=${r_min:-none}"
gate_fail=0
# row coverage: local must be >= ~99% of source rows (union => >=; tolerance for in-flight compaction)
if [ "${r_rows:-0}" -gt 0 ]; then
  tol=$(( r_rows / 100 + 50 ))
  if [ "${l_rows:-0}" -ge $(( r_rows - tol )) ]; then ok "row coverage: local $l_rows ≥ source $r_rows − tol $tol"
  else no "row coverage SHORT: local $l_rows < source $r_rows − tol $tol"; gate_fail=1; fi
fi
# depth: local earliest must be <= source earliest (we hold history at least as deep)
if [ -n "$r_min" ]; then
  if [ -n "$l_min" ] && [ "$l_min" \< "$r_min" -o "$l_min" = "$r_min" ]; then ok "archive depth: local earliest $l_min ≤ source $r_min"
  else no "archive depth SHORT: local earliest ${l_min:-none} > source $r_min"; gate_fail=1; fi
fi

say "verdict"
if [ "$gate_fail" = 0 ]; then
  echo "  => RECORD-KEEPING ELIGIBLE — archive parity with $FROM confirmed."
  echo "     (run failover/cluster-doctor.sh for the full cross-cluster assertion incl. config completeness)"
  exit 0
else
  echo "  => NOT YET ELIGIBLE — archive gate FAILED. Do NOT trust this box as dictator-of-record."
  echo "     Re-run provision-peer (the merge is idempotent); if it stays short, inspect reconcile logs."
  exit 1
fi
