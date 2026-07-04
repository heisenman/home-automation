#!/usr/bin/env bash
# Provision THIS host as the LAN NTP anchor (chrony serve). Idempotent; reusable on the next dictator box.
#
# Why: edge nodes need a wall clock (TLS/SNTP). The dictator serves time so they don't depend on the
# internet — critical once the LAN is air-gapped behind the OpenWRT router. See README.md + the coord
# task `dictator-ntp-serve`. GATED production write (installs a package) — run with Hugh's OK.
#
# Usage:  sudo provisioning/ntp/install-ntp-serve.sh [LAN_CIDR]      # default 192.168.0.0/24
set -euo pipefail
CIDR="${1:-192.168.0.0/24}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF=/etc/chrony/conf.d/ha-serve.conf

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }

if ! command -v chronyd >/dev/null 2>&1; then
  echo "== installing chrony =="; apt-get update -y; apt-get install -y chrony
fi

# chrony takes over the client role from systemd-timesyncd; make sure only one disciplines the clock.
systemctl disable --now systemd-timesyncd 2>/dev/null || true

install -m 0644 "$HERE/chrony-ha-serve.conf" "$CONF"
sed -i "s#^allow .*#allow ${CIDR}#" "$CONF"     # honor a non-default CIDR

# Ensure the default chrony.conf actually sources conf.d/ (Debian ships `confdir /etc/chrony/conf.d`).
if ! grep -qE '^\s*(confdir|sourcedir)\s+/etc/chrony/conf\.d' /etc/chrony/chrony.conf; then
  echo "confdir /etc/chrony/conf.d" >> /etc/chrony/chrony.conf
fi

systemctl enable --now chrony
systemctl restart chrony
sleep 1

echo "== verify: serving on udp:123 =="
ss -ulpn | grep -q ':123' || { echo "FAIL: nothing listening on udp:123"; exit 1; }
chronyc -n serverstats 2>/dev/null || true
echo "OK: $(hostname) is serving NTP to ${CIDR}"
