#!/usr/bin/env bash
# cluster-doctor — READ-ONLY health/invariant + capability checker for the dictator↔failover cluster.
# Asserts the safety invariants and a capability preflight; makes NO changes. Run on demand, and
# especially AFTER a failover (or before promoting/joining a box). Exit 0 = all green, 1 = a FAIL.
#
#   env: PRIMARY_HOST(=210) STANDBY_HOST(=245) VIP(=.200) BROKER(=VIP) CLUSTER_KEY(ssh key, optional)
#        HEARTBEAT_FRESH(=12s)
# Synthesis #1 (docs/retro/dev-retro-synthesis.md): "invariant-first, verify state on the bus."
set -uo pipefail
PRIMARY="${PRIMARY_HOST:-192.168.0.210}"
STANDBY="${STANDBY_HOST:-192.168.0.245}"
VIP="${VIP:-192.168.0.200}"
BROKER="${BROKER:-$VIP}"
HEARTBEAT_FRESH="${HEARTBEAT_FRESH:-12}"
KEY="${CLUSTER_KEY:-$HOME/.ssh/id_cluster}"   # default to the cluster key like the other failover scripts (else SSH-to-peer false-fails)
REPO_REMOTE="${REPO_REMOTE:-/home/visko/home_automation}"
SSH(){ ssh ${KEY:+-i "$KEY"} -o BatchMode=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new "$@"; }
SELF_IPS=" $(hostname -I 2>/dev/null) "
is_self(){ [[ "$SELF_IPS" == *" $1 "* ]]; }
# Gather a node's facts: run LOCALLY when the target is this host (self-SSH often isn't authorized and
# would false-fail as 'unreachable' -> bogus no-dictator/split-brain alarms), else over the cluster key.
on(){ local h="$1"; shift; if is_self "$h"; then bash -c "$*" 2>/dev/null; else SSH "visko@$h" "$@" 2>/dev/null; fi; }

pass=0; fail=0; warn=0
ok(){ printf '  [PASS] %s\n' "$*"; pass=$((pass+1)); }
no(){ printf '  [FAIL] %s\n' "$*"; fail=$((fail+1)); }
wn(){ printf '  [WARN] %s\n' "$*"; warn=$((warn+1)); }
hdr(){ printf '\n== %s ==\n' "$*"; }

now=$(date +%s)
echo "cluster-doctor $(date -Is)  primary=$PRIMARY standby=$STANDBY vip=$VIP"

# ---- gather per-node facts (one SSH round-trip each) ----
declare -A CTRL VIPH KA SQL REACH
for h in "$PRIMARY" "$STANDBY"; do
  if ! out=$(on "$h" "echo REACH=yes;
      echo CTRL=\$(systemctl is-active ha-controller 2>/dev/null);
      ip -o addr show 2>/dev/null | grep -qw $VIP && echo VIPH=yes || echo VIPH=no;
      echo KA=\$(systemctl is-active keepalived 2>/dev/null);
      command -v sqlite3 >/dev/null && echo SQL=yes || echo SQL=no"); then
    REACH[$h]=no; CTRL[$h]=unknown; VIPH[$h]=unknown; KA[$h]=unknown; SQL[$h]=unknown
    continue
  fi
  REACH[$h]=yes
  CTRL[$h]=$(sed -n 's/^CTRL=//p' <<<"$out"); VIPH[$h]=$(sed -n 's/^VIPH=//p' <<<"$out")
  KA[$h]=$(sed -n 's/^KA=//p' <<<"$out");     SQL[$h]=$(sed -n 's/^SQL=//p' <<<"$out")
done

# ---- INVARIANTS (the safety properties that must always hold) ----
hdr "Invariants"
ctrl_count=0; ctrl_node=""
for h in "$PRIMARY" "$STANDBY"; do [ "${CTRL[$h]}" = active ] && { ctrl_count=$((ctrl_count+1)); ctrl_node="$h"; }; done
case "$ctrl_count" in
  1) ok "exactly one ha-controller active (on $ctrl_node)";;
  0) no "ZERO ha-controller active — NOBODY is the dictator (control is down)";;
  *) no "TWO ha-controllers active — SPLIT BRAIN ($PRIMARY=${CTRL[$PRIMARY]} $STANDBY=${CTRL[$STANDBY]})";;
esac

vip_count=0; vip_node=""
for h in "$PRIMARY" "$STANDBY"; do [ "${VIPH[$h]}" = yes ] && { vip_count=$((vip_count+1)); vip_node="$h"; }; done
case "$vip_count" in
  1) ok "exactly one node holds VIP $VIP (on $vip_node)";;
  0) no "VIP $VIP held by NOBODY (clients can't reach the dictator)";;
  *) no "VIP $VIP held by MULTIPLE nodes — split brain / ARP conflict";;
esac

if [ "$ctrl_count" = 1 ] && [ "$vip_count" = 1 ]; then
  [ "$ctrl_node" = "$vip_node" ] && ok "dictator coherent: VIP holder == controller node ($ctrl_node)" \
                                 || no "INCOHERENT: VIP on $vip_node but controller on $ctrl_node"
fi

# ---- heartbeats on the bus (verify state on the bus, not just systemd) ----
hdr "Cluster bus heartbeats ($BROKER)"
for node in 210 245; do
  msg=$(timeout 5 mosquitto_sub -h "$BROKER" -t "ha/cluster/$node/heartbeat" -C 1 -W 4 2>/dev/null)
  if [ -z "$msg" ]; then wn "no heartbeat retained for node $node"; continue; fi
  ts=$(sed -n 's/.*"ts":[[:space:]]*\([0-9]\{1,\}\).*/\1/p' <<<"$msg"); age=$(( now - ${ts:-0} ))
  ca=$(grep -o '"controller_active":[[:space:]]*\(true\|false\)' <<<"$msg" | grep -o 'true\|false')
  if [ -n "$ts" ] && [ "$age" -lt "$HEARTBEAT_FRESH" ]; then ok "node $node heartbeat fresh (${age}s, controller_active=$ca)"
  else wn "node $node heartbeat STALE (${age}s > ${HEARTBEAT_FRESH}s) — publisher down? (controller_active=$ca)"; fi
done

# ---- capability preflight (a box must have these to safely hold/seize a role) ----
hdr "Capability preflight"
for h in "$PRIMARY" "$STANDBY"; do
  [ "${REACH[$h]}" = yes ] && ok "$h reachable over SSH" || { no "$h UNREACHABLE over SSH"; continue; }
  [ "${KA[$h]}" = active ] && ok "$h keepalived active" || wn "$h keepalived ${KA[$h]} (ok if not yet gone live)"
  [ "${SQL[$h]}" = yes ] && ok "$h sqlite3 present (consistent DB snapshots)" || wn "$h sqlite3 MISSING (raw-copy fallback only)"
done
# VIP reachability from where this runs (the segment-aware check the retro called out)
if timeout 4 bash -c "cat </dev/null >/dev/tcp/${VIP}/1883" 2>/dev/null; then ok "VIP $VIP:1883 reachable from $(hostname) ($(hostname -I 2>/dev/null|awk '{print $1}'))"
else no "VIP $VIP:1883 NOT reachable from this host's segment"; fi
# cluster SSH bidirectional (fence/sync transport)
if [ "${REACH[$PRIMARY]}" = yes ] && [ "${REACH[$STANDBY]}" = yes ]; then
  on "$PRIMARY" "ssh -i ~/.ssh/id_cluster -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new visko@$STANDBY true" \
     && ok "cluster SSH $PRIMARY -> $STANDBY (fence/sync path)" || wn "cluster SSH $PRIMARY -> $STANDBY failed (id_cluster?)"
fi

# ---- dictator config completeness (the 2026-06-24 control_secrets.yaml gap) ----
# Every dictator-CAPABLE box (primary AND standby) must hold the full critical config/secret set, or a
# promote/takeover boots a controller that can't actuate (issuer -> unknown-device). Source of truth is
# failover/dictator-files.manifest; here we assert PRESENCE + non-empty on each reachable box.
hdr "Dictator config completeness"
MANIFEST="$(dirname "$0")/dictator-files.manifest"
if [ ! -f "$MANIFEST" ]; then
  wn "no dictator-files.manifest beside cluster-doctor — skipping config-completeness check"
else
  mapfile -t crit_files < <(awk -F'|' '/^[[:space:]]*#/ || NF<3 {next}
                                       {gsub(/^[[:space:]]+|[[:space:]]+$/,"",$1); gsub(/^[[:space:]]+|[[:space:]]+$/,"",$2)
                                        if ($2=="critical") print $1}' "$MANIFEST")
  for h in "$PRIMARY" "$STANDBY"; do
    [ "${REACH[$h]}" = yes ] || { wn "$h unreachable — can't audit dictator config"; continue; }
    miss=""
    for f in "${crit_files[@]}"; do on "$h" "test -s $REPO_REMOTE/$f" || miss="$miss $f"; done
    if [ -z "$miss" ]; then ok "$h has all ${#crit_files[@]} critical dictator files"
    else no "$h MISSING critical dictator file(s):$miss — a promote/takeover here can't actuate (unknown-device)"; fi
  done
fi

# ---- history-reconcile deadline (ADR-0016): standby divergence gap vs the device-pull net ----
# The non-VIP box is frozen (it doesn't ingest); sync-standby does NOT copy hot.db, so the age of its
# newest reading == how long its history has diverged. Once that exceeds the smallest device buffer,
# on-device buffer-pull can no longer heal the oldest slice of the gap -> a failover now would rely
# entirely on the (still-deferred) parquet cross-box deep-reconcile. Surface that as a deadline.
hdr "History-reconcile deadline (ADR-0016)"
# shellcheck disable=SC1091
. "$(dirname "$0")/device-buffers.env" 2>/dev/null || true
MIN_DEVICE_BUFFER_S=${MIN_DEVICE_BUFFER_S:-5875200}   # ~68 d (SwitchBot) fallback if the env is absent
standby_node=""
for h in "$PRIMARY" "$STANDBY"; do [ "${VIPH[$h]}" = no ] && standby_node="$h"; done
if [ -z "$standby_node" ]; then
  wn "no clear non-VIP box (unreachable or split) — skipping divergence-gap check"
else
  maxts=$(on "$standby_node" "sqlite3 ~/home_automation/instance/db/hot.db 'SELECT MAX(ts) FROM readings;'")
  if [ -z "$maxts" ]; then
    wn "standby $standby_node: no readings in hot.db to gauge the divergence gap (fresh box?) — skipping"
  else
    epoch=$(date -d "$maxts" +%s 2>/dev/null)
    if [ -z "$epoch" ]; then
      wn "standby $standby_node: couldn't parse newest reading ts '$maxts' — skipping"
    else
      gap=$(( now - epoch )); gd=$(( gap / 86400 )); md=$(( MIN_DEVICE_BUFFER_S / 86400 ))
      if [ "$gap" -le "$MIN_DEVICE_BUFFER_S" ]; then
        ok "standby $standby_node divergence gap ${gd}d within device-pull net (min buffer ${md}d) — a failover now is buffer-recoverable"
      else
        wn "standby $standby_node divergence gap ${gd}d EXCEEDS min device-buffer ${md}d — buffer-pull can no longer heal the oldest slice; a failover now needs the parquet deep-reconcile (ADR-0016, still deferred). Run reconcile-history before a swap, or accept the loss."
      fi
    fi
  fi
fi

# ---- history convergence (ADR-0016): did reconcile actually merge the window across boxes? ----
# The SETTLED part of the divergence window (older than ~1h, well past the 15-min reconcile interval) must
# match on both boxes — the proactive loop + the notify.sh transition hook should have merged it
# bidirectionally. The most-recent slice is excluded: the standby legitimately lags up to one interval
# before the next proactive push, so counting it would false-alarm.
hdr "History convergence (ADR-0016)"
rc_cut=$(date -u -d "yesterday 00:00:00" +%Y-%m-%dT00:00:00Z 2>/dev/null || date -u -v-1d +%Y-%m-%dT00:00:00Z 2>/dev/null)
rc_settled=$(date -u -d "1 hour ago" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-1H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)
if [ -z "$rc_cut" ] || [ -z "$rc_settled" ]; then
  wn "couldn't compute reconcile window bounds (date) — skipping convergence check"
else
  cA=$(on "$PRIMARY" "sqlite3 ~/home_automation/instance/db/hot.db \"SELECT COUNT(*) FROM readings WHERE ts>='$rc_cut' AND ts<'$rc_settled';\"")
  cB=$(on "$STANDBY" "sqlite3 ~/home_automation/instance/db/hot.db \"SELECT COUNT(*) FROM readings WHERE ts>='$rc_cut' AND ts<'$rc_settled';\"")
  if ! [[ "$cA" =~ ^[0-9]+$ && "$cB" =~ ^[0-9]+$ ]]; then
    wn "couldn't read settled-window counts on both boxes (unreachable?) — skipping convergence check"
  else
    hi=$cA; [ "$cB" -gt "$hi" ] && hi=$cB
    diff=$(( cA > cB ? cA - cB : cB - cA )); tol=$(( hi / 50 + 5 ))   # ~2% + small floor
    if [ "$diff" -le "$tol" ]; then
      ok "settled-window readings converged: primary=$cA standby=$cB (Δ$diff ≤ tol $tol)"
    else
      wn "settled-window readings DIVERGE: primary=$cA standby=$cB (Δ$diff > tol $tol) — reconcile-history may not be running; a failover now would leave a history hole. Check ha-reconcile-history + /var/log/ha-reconcile.log"
    fi
  fi
fi

# ---- archive completeness (ADR-0018): the parquet ARCHIVE must converge, not just the hot tier ----
# Record-keeping HARD GATE. The hot-tier checks above cover ~today; this asserts the months-deep parquet
# archive matches across dictator-capable boxes. A box elevated to dictator WITHOUT the archive (the
# 2026-06-25 incident: 210 had ~1.5 d while .245 held since January) serves a truncated record-of-truth.
# After reconcile-parquet they must converge; a material shortfall on either box FAILs.
hdr "Archive completeness (ADR-0018)"
arch_stats(){ # $1=host -> "rows|earliest" from that box's parquet archive (via its venv duckdb)
  local pyexpr='import glob,duckdb,sys
f=[x for x in glob.glob(sys.argv[1]+"/**/*.parquet",recursive=True) if "/year=0/" not in x]
print(("0|") if not f else "{}|{}".format(*duckdb.connect().execute(f"SELECT COUNT(*),MIN(ts) FROM read_parquet({f!r},union_by_name=true)").fetchone()).replace("None",""))'
  on "$1" "$REPO_REMOTE/venv/bin/python3 -c '$pyexpr' '$REPO_REMOTE/instance/db/parquet'"
}
if [ "${REACH[$PRIMARY]}" = yes ] && [ "${REACH[$STANDBY]}" = yes ]; then
  pStat=$(arch_stats "$PRIMARY"); sStat=$(arch_stats "$STANDBY")
  pRows="${pStat%%|*}"; pMin="${pStat#*|}"; sRows="${sStat%%|*}"; sMin="${sStat#*|}"
  if ! [[ "$pRows" =~ ^[0-9]+$ && "$sRows" =~ ^[0-9]+$ ]]; then
    wn "couldn't read parquet archive stats on both boxes (duckdb/venv missing?) — skipping archive gate"
  else
    hi=$pRows; [ "$sRows" -gt "$hi" ] && hi=$sRows
    tol=$(( hi / 100 + 50 ))                         # ~1% + floor (absorbs in-flight compaction)
    diff=$(( pRows > sRows ? pRows - sRows : sRows - pRows ))
    echo "  primary=$PRIMARY rows=$pRows earliest=${pMin:-none} | standby=$STANDBY rows=$sRows earliest=${sMin:-none}"
    if [ "$diff" -le "$tol" ]; then ok "parquet archives converged (Δ$diff ≤ tol $tol rows)"
    else no "parquet archives DIVERGE (Δ$diff > tol $tol) — a box is missing archive the peer holds. Run failover/reconcile-parquet.sh --once (or provision-peer). A failover to the thin box now serves truncated history."; fi
    # depth: neither dictator-capable box should be materially shallower than the other
    if [ -n "$pMin" ] && [ -n "$sMin" ] && [ "$pMin" != "$sMin" ]; then
      shallow=$PRIMARY; deep=$sMin; [ "$pMin" \> "$sMin" ] || { shallow=$STANDBY; deep=$pMin; }
      wn "archive DEPTH differs: earliest primary=$pMin standby=$sMin — $shallow is shallower (deepest=$deep). reconcile-parquet to seed the gap."
    fi
  fi
else
  wn "both boxes not reachable — skipping archive completeness gate"
fi

# ---- verdict ----
hdr "Verdict"
printf '  %d pass, %d warn, %d FAIL\n' "$pass" "$warn" "$fail"
[ "$fail" -eq 0 ] && { echo "  => HEALTHY"; exit 0; } || { echo "  => UNHEALTHY (see FAILs)"; exit 1; }
