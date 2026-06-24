#!/usr/bin/env bash
# keepalived track_script body. Exit 0 if THIS box is FIT to be dictator, non-zero otherwise.
# Fit = ha-api responding AND the Midea reachable on the LAN. An unfit MASTER loses 'weight'
# priority (see keepalived.conf.tmpl) -> the healthy standby takes over.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
[ -f "$REPO/instance/cluster.env" ] && . "$REPO/instance/cluster.env"
: "${API:=http://localhost:8123}"

# 1. local ha-api up (proves the stack is alive)
curl -fsS --max-time 4 "$API/api/v1/sensors" >/dev/null 2>&1 || exit 1

# 2. Midea reachable on the LAN (can we actually actuate?)
ENVF="$REPO/instance/midea-device.env"
if [ -f "$ENVF" ]; then
  MIP=$(grep -E '^MIDEA_IP=' "$ENVF" | head -1 | cut -d= -f2- | tr -d "\"' ")
  if [ -n "$MIP" ]; then ping -c1 -W2 "$MIP" >/dev/null 2>&1 || exit 2; fi
fi
exit 0
