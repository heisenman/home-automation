#!/usr/bin/env bash
# provisioning/airgap/verify-gap.sh — assert the AIR GAP holds. Run ON .210 (post-move). Also intended as
# a boot-time systemd oneshot: the gap is a userspace policy (gotcha 1) — assert it, never assume it.
set -uo pipefail
HH_IFACE="${HH_IFACE:-enp4s0}"; HH_NET="${HH_NET:-192.168.0}"
AG_IFACE="${AG_IFACE:-eno1}";   AG_NET="${AG_NET:-192.168.1}"
HA2="${HA2:-192.168.1.210}"; ROUTER="${ROUTER:-192.168.1.1}"
KEY="${CLUSTER_KEY:-/home/visko/.ssh/id_cluster}"
SSHO="-i $KEY -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=6"
pass=0; fail=0; skip=0
ok(){ printf '  \033[32m✓\033[0m %s\n' "$1"; pass=$((pass+1)); }
no(){ printf '  \033[31m✗\033[0m %s\n' "$1"; fail=$((fail+1)); }
sk(){ printf '  \033[33m⧗\033[0m %s\n' "$1"; skip=$((skip+1)); }

echo "== air-gap invariants (.210) =="
[ "$(cat /proc/sys/net/ipv4/ip_forward)" = 0 ] && ok "net.ipv4.ip_forward = 0 (no routing between legs — THE gap)" || no "ip_forward != 0 — GAP BREACHED"
ip -4 addr show "$HH_IFACE" 2>/dev/null | grep -q "inet ${HH_NET}\." && ok "household leg $HH_IFACE on ${HH_NET}.x" || no "no household IP on $HH_IFACE"
ip -4 addr show "$AG_IFACE" 2>/dev/null | grep -q "inet ${AG_NET}\." && ok "air-gap leg $AG_IFACE on ${AG_NET}.x" || no "no air-gap IP on $AG_IFACE"
ip route show default | grep -q "dev $AG_IFACE" && no "default route via the air-gap leg (must be household)" || ok "default route not via the air-gap leg"

echo "== reachability =="
ping -c1 -W2 -I "$AG_IFACE" "$ROUTER" >/dev/null 2>&1 && ok "air-gap router $ROUTER reachable via $AG_IFACE" || no "air-gap router $ROUTER unreachable"
ping -c1 -W2 -I "$AG_IFACE" "$HA2" >/dev/null 2>&1 && ok "ha-2 $HA2 reachable via $AG_IFACE" || no "ha-2 $HA2 unreachable"

echo "== NEGATIVE test (the real gap proof): from ha-2, the household net MUST be unreachable =="
ha2_can_reach_hh(){ ssh $SSHO "visko@$HA2" "ping -c1 -W2 ${HH_NET}.1" >/dev/null 2>&1; }
if ssh $SSHO "visko@$HA2" true 2>/dev/null; then
  if ha2_can_reach_hh; then no "ha-2 CAN reach the household net (${HH_NET}.1) — GAP BREACHED"
  else ok "ha-2 cannot reach the household net (${HH_NET}.x) — gap holds"; fi
else
  sk "ha-2 negative-reachability test skipped (ha-2 not reachable yet)"
fi

echo
if [ "$fail" -eq 0 ]; then printf '\033[32m✓ GAP INTACT\033[0m (%d checks, %d skipped)\n' "$pass" "$skip"; exit 0
else printf '\033[31m✗ GAP has %d breach(es)\033[0m — investigate before proceeding.\n' "$fail"; exit 1; fi
