#!/usr/bin/env bash
# Idempotent per-box installer for the failover cluster. Run on EACH box as visko (uses sudo internally).
# Prereqs:
#   1. instance/cluster.env present  (cp failover/cluster.env.example -> instance/cluster.env; set ROLE/PEER_HOST)
#   2. keepalived installed          (sudo apt install -y keepalived)
#   3. cluster SSH key working        (failover/ setup already cross-installed ~/.ssh/id_cluster)
# Does NOT start keepalived — prints the supervised go-live step at the end (see failover-runbook.md).
set -euo pipefail
export PATH="/usr/sbin:/sbin:$PATH"   # keepalived/ip live in sbin — not always on a non-login PATH
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
[ -f "$REPO/instance/cluster.env" ] || { echo "ERROR: copy failover/cluster.env.example -> instance/cluster.env, set ROLE/PEER_HOST"; exit 1; }
. "$REPO/instance/cluster.env"
: "${ROLE:?set ROLE in instance/cluster.env}"

case "$ROLE" in
  primary) STATE=MASTER; PRIORITY=150;;
  standby) STATE=BACKUP; PRIORITY=100;;
  *) echo "ERROR: ROLE must be primary|standby (got '$ROLE')"; exit 1;;
esac
IFACE=$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}')
[ -n "$IFACE" ] || { echo "ERROR: could not detect default-route interface"; exit 1; }
echo "==> role=$ROLE  state=$STATE  priority=$PRIORITY  iface=$IFACE"

chmod +x "$REPO"/failover/*.sh

command -v keepalived >/dev/null || { echo "ERROR: keepalived not installed -> sudo apt install -y keepalived"; exit 1; }
sudo mkdir -p /etc/keepalived
sed -e "s/@STATE@/$STATE/" -e "s/@PRIORITY@/$PRIORITY/" -e "s#@IFACE@#$IFACE#" \
    "$REPO/failover/keepalived.conf.tmpl" | sudo tee /etc/keepalived/keepalived.conf >/dev/null
echo "    wrote /etc/keepalived/keepalived.conf"

# ha-controller unit must EXIST so notify.sh can start it on takeover. Install (disabled on standby).
if ! systemctl cat ha-controller >/dev/null 2>&1; then
  sudo cp "$REPO/systemd/ha-controller.service" /etc/systemd/system/ha-controller.service
  sudo systemctl daemon-reload
  echo "    installed ha-controller.service"
fi
if [ "$ROLE" = standby ]; then
  sudo systemctl disable ha-controller >/dev/null 2>&1 || true
  for u in ha-primary-watch.service ha-standby-sync.service ha-standby-sync.timer; do
    sudo cp "$REPO/failover/systemd/$u" "/etc/systemd/system/$u"
  done
  sudo systemctl daemon-reload
  echo "    installed primary-watch + sync units (disabled until go-live)"
fi

sudo touch /var/log/ha-failover.log; sudo chown visko /var/log/ha-failover.log 2>/dev/null || true

echo ""
echo "==> deployed (NOTHING started). Supervised go-live (see failover/failover-runbook.md):"
echo "    PRIMARY first, then STANDBY. On this box:"
echo "    sudo systemctl enable --now keepalived"
[ "$ROLE" = standby ] && echo "    sudo systemctl enable --now ha-primary-watch.service ha-standby-sync.timer"
