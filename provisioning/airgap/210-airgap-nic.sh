#!/usr/bin/env bash
# provisioning/airgap/210-airgap-nic.sh — bring .210's AIR-GAP leg (eno1) up on 192.168.1.0/24, and
# guarantee it is a LEAF, not a route: the air gap is `net.ipv4.ip_forward=0` + NO cross-subnet route +
# NO default route via eno1 (the household enp4s0 keeps the default). Idempotent + reversible + persistent
# (ifupdown drop-in so it survives reboot). Run ON .210.
#
# Usage:  210-airgap-nic.sh            bring up + persist
#         210-airgap-nic.sh --down     tear down (remove address + drop-in)
set -euo pipefail
IFACE="${AIRGAP_IFACE:-eno1}"
ADDR="${AIRGAP_ADDR:-192.168.1.245}"
CIDR="${AIRGAP_CIDR:-24}"
DROPIN="/etc/network/interfaces.d/airgap-${IFACE}"

if [ "${1:-}" = "--down" ]; then
  sudo -n rm -f "$DROPIN"
  sudo -n ip addr flush dev "$IFACE" 2>/dev/null || true
  sudo -n ip link set "$IFACE" down 2>/dev/null || true
  echo "air-gap leg $IFACE torn down (drop-in removed, iface down)"; exit 0
fi

# persistent ifupdown drop-in (matches .210's existing /etc/network/interfaces model) — NO gateway line
sudo -n tee "$DROPIN" >/dev/null <<EOF
# air-gap leg — .210's foot on the isolated 192.168.1.0/24 net. NO gateway (household keeps default route).
auto ${IFACE}
iface ${IFACE} inet static
    address ${ADDR}/${CIDR}
    post-up sysctl -w net.ipv4.ip_forward=0
EOF

# apply now (runtime), idempotent
sudo -n ip link set "$IFACE" up
sudo -n ip addr replace "${ADDR}/${CIDR}" dev "$IFACE"
sudo -n sysctl -w net.ipv4.ip_forward=0 >/dev/null
sudo -n ip route del default dev "$IFACE" 2>/dev/null || true   # never a default via the air-gap leg

echo "air-gap leg UP: ${IFACE} = ${ADDR}/${CIDR}"
echo "  ip_forward = $(cat /proc/sys/net/ipv4/ip_forward)  (MUST be 0 — this is the gap)"
echo "  default route stays on the household NIC:"
ip route show default | sed 's/^/    /'
ip -br addr show "$IFACE" | sed 's/^/  /'
