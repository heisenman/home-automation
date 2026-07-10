#!/usr/bin/env bash
# Install the air-gap relay (ADR-0033 Phase 1b) on the dual-homed bridge (.210):
#   - a dedicated minimal MQTT broker (port 1884, isolated from dev/failover brokers)
#   - the relay daemon in its OWN venv (esphome-proof), as an unprivileged `ha-relay` user
# Idempotent. Run with sudo. Generates relay creds on first run (printed once for the ha-2 client).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== 1. relay user =="
id ha-relay >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin ha-relay

echo "== 2. own venv (paho only) =="
install -d -o root -g root /opt/ha-relay
if [ ! -x /opt/ha-relay/venv/bin/python ]; then
  python3 -m venv /opt/ha-relay/venv
  /opt/ha-relay/venv/bin/pip -q install --upgrade pip
  /opt/ha-relay/venv/bin/pip -q install 'paho-mqtt>=1.6'
fi
install -m 0644 "$HERE/relayd.py" /opt/ha-relay/relayd.py

echo "== 3. broker config + ACL + creds =="
install -m 0644 "$HERE/relay-broker.conf" /etc/mosquitto/relay-broker.conf
install -m 0644 "$HERE/relay.acl"        /etc/mosquitto/relay.acl
PASS_FILE=/etc/mosquitto/relay.passwd
CRED_OUT=""
if [ ! -f "$PASS_FILE" ]; then
  RELAYD_PW="$(openssl rand -base64 18)"
  HA2_PW="$(openssl rand -base64 18)"
  touch "$PASS_FILE"
  mosquitto_passwd -b "$PASS_FILE" relayd "$RELAYD_PW"
  mosquitto_passwd -b "$PASS_FILE" ha2    "$HA2_PW"
  CRED_OUT="relayd:${RELAYD_PW}  ha2:${HA2_PW}"
  # daemon env (relayd creds)
  install -d /etc/ha-relay
  cat > /etc/ha-relay/relay.env <<EOF
RELAY_BROKER_HOST=localhost
RELAY_BROKER_PORT=1884
RELAY_BROKER_USER=relayd
RELAY_BROKER_PASS=${RELAYD_PW}
RELAY_RATE_PER_MIN=30
EOF
  chmod 0640 /etc/ha-relay/relay.env
fi
chown mosquitto:root "$PASS_FILE"; chmod 0640 "$PASS_FILE"

echo "== 4. systemd units =="
install -m 0644 "$HERE/systemd/ha-relay-broker.service" /etc/systemd/system/ha-relay-broker.service
install -m 0644 "$HERE/systemd/ha-relay.service"        /etc/systemd/system/ha-relay.service
systemctl daemon-reload
systemctl enable --now ha-relay-broker.service
sleep 1
systemctl enable --now ha-relay.service
sleep 2

echo "== 5. verify =="
ss -tlnp 2>/dev/null | grep -q ':1884' && echo "OK: relay broker listening on 1884" || { echo "FAIL: 1884 not listening"; exit 1; }
systemctl is-active --quiet ha-relay.service && echo "OK: ha-relay daemon active" || { echo "FAIL: ha-relay not active"; journalctl -u ha-relay -n 20 --no-pager; exit 1; }
[ -n "$CRED_OUT" ] && { echo; echo "== relay credentials (store now; shown once) =="; echo "  $CRED_OUT"; echo "  (ha-2 client uses the 'ha2' identity to publish relay/+/request)"; }
echo "OK: relay installed."
