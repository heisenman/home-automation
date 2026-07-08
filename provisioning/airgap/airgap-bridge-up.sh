#!/bin/sh
# provisioning/airgap/airgap-bridge-up.sh — .210's persistent WiFi air-gap bridge leg.
# Brings wlp2s0 up on the air-gap net (static, NO gateway, NO forwarding) and then supervises
# wpa_supplicant in the FOREGROUND so ha-airgap-bridge.service restarts it if the radio hiccups.
# NO secrets here — the PSK lives HASHED in the wpa conf. See docs/airgap/MIGRATION-DESIGN-LOG.md (DJ-15).
set -eu
IFACE="${AIRGAP_WIFI_IFACE:-wlp2s0}"
ADDR="${AIRGAP_WIFI_ADDR:-192.168.1.245/24}"
WPACONF="${AIRGAP_WPA_CONF:-/etc/wpa_supplicant/wpa_supplicant-${IFACE}.conf}"

ip link set "$IFACE" up
ip addr replace "$ADDR" dev "$IFACE"
sysctl -w net.ipv4.ip_forward=0 >/dev/null              # THE gap — never forward between the two legs
ip route del default dev "$IFACE" 2>/dev/null || true   # never a default route via the air-gap leg
exec wpa_supplicant -i "$IFACE" -c "$WPACONF"           # foreground; systemd supervises + restarts it
