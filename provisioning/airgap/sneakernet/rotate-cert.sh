#!/usr/bin/env bash
# Rotate the air-gap TLS cert (ADR-0033 D1) — long-dated self-signed, no ACME. Backs up the current cert,
# regenerates for 10 years, and reminds to redeploy. Pairs with ha-cert-monitor's T-12mo alert.
#   rotate-cert.sh [days]     # default 3650
set -euo pipefail
DAYS="${1:-3650}"
ROOT="${HA_ROOT:-$HOME/home_automation}"
cd "$ROOT"
CRT=instance/tls/server.crt
[ -f "$CRT" ] && cp -a "$CRT" "$CRT.bak-$(date -u +%Y%m%dT%H%M%SZ 2>/dev/null || date +%s)"
echo "== regenerating $CRT for ${DAYS} days =="
venv/bin/python tools/gen_tls.py --days "$DAYS"
echo "new expiry: $(openssl x509 -in "$CRT" -noout -enddate | cut -d= -f2)"
echo "REDEPLOY: restart ha-api-tls (and the .210 nginx bridge if it terminates with this cert);"
echo "          re-trust on clients that pinned the old self-signed cert (browsers/panels)."
