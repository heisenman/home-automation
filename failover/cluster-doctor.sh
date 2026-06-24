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
KEY="${CLUSTER_KEY:-}"
SSH(){ ssh ${KEY:+-i "$KEY"} -o BatchMode=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new "$@"; }
on(){ local h="$1"; shift; SSH "visko@$h" "$@" 2>/dev/null; }

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

# ---- verdict ----
hdr "Verdict"
printf '  %d pass, %d warn, %d FAIL\n' "$pass" "$warn" "$fail"
[ "$fail" -eq 0 ] && { echo "  => HEALTHY"; exit 0; } || { echo "  => UNHEALTHY (see FAILs)"; exit 1; }
