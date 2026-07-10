#!/usr/bin/env bash
# Cert-expiry monitor (ADR-0033 D1). The air-gap TLS cert is self-signed with no ACME renewal, so an
# unnoticed expiry = total HTTPS outage (PWA + ha-api-tls). This fires a system alert well ahead of time.
#
#   check-cert-expiry.sh [cert-path] [warn-days]
# Defaults: instance/tls/server.crt, 365 days. Publishes home/_alert/new when inside the warn window,
# so it rides the existing alert/ntfy path. Idempotent; run daily via ha-cert-monitor.timer.
set -euo pipefail
CERT="${1:-instance/tls/server.crt}"
WARN_DAYS="${2:-365}"
BROKER="${HA_BROKER:-127.0.0.1}"
[ -r "$CERT" ] || { echo "no cert at $CERT"; exit 0; }

END_EPOCH=$(date -d "$(openssl x509 -in "$CERT" -noout -enddate | cut -d= -f2)" +%s)
NOW=$(date +%s)
DAYS=$(( (END_EPOCH - NOW) / 86400 ))
echo "cert $CERT expires in ${DAYS}d ($(openssl x509 -in "$CERT" -noout -enddate | cut -d= -f2))"

if [ "$DAYS" -le "$WARN_DAYS" ]; then
  SEV="warning"; [ "$DAYS" -le 30 ] && SEV="critical"
  MSG="TLS cert ${CERT##*/} expires in ${DAYS} days — regenerate (tools/gen_tls.py --days 3650) + redeploy; no ACME on the air-gap."
  echo "ALERT($SEV): $MSG"
  command -v mosquitto_pub >/dev/null 2>&1 && mosquitto_pub -h "$BROKER" -t 'home/_alert/new' \
    -m "{\"kind\":\"cert_expiry\",\"severity\":\"$SEV\",\"device_id\":\"_system\",\"name\":\"TLS cert\",\"detail\":\"$MSG\"}" \
    2>/dev/null || echo "(mosquitto_pub unavailable — alert logged only)"
fi
