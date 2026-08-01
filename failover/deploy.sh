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
: "${VRID:=51}"; : "${CIDR:=24}"          # per-cluster; defaults = the household values (backward-compat)
: "${VIP:?set VIP in instance/cluster.env}"
# IFACE: honor an explicit setting — required on the dual-homed .210, where the default-route leg is the
# WRONG interface for the air-gap cluster. Otherwise auto-detect the default-route interface.
IFACE="${IFACE:-$(ip route show default 2>/dev/null | awk '/default/{print $5; exit}')}"
[ -n "$IFACE" ] || { echo "ERROR: could not resolve IFACE (set it in instance/cluster.env)"; exit 1; }
echo "==> role=$ROLE state=$STATE priority=$PRIORITY iface=$IFACE vip=$VIP/$CIDR vrid=$VRID net=${NET_NAME:-household}"

chmod +x "$REPO"/failover/*.sh

command -v keepalived >/dev/null || { echo "ERROR: keepalived not installed -> sudo apt install -y keepalived"; exit 1; }
sudo mkdir -p /etc/keepalived

# GUARD: the template renders exactly ONE vrrp_instance, but a dual-homed box can legitimately run TWO
# legs from one keepalived (.210 straddles the household VIP .0.200 and the air-gap VIP .1.200 with
# distinct VRIDs/interfaces — the second block is hand-assembled, not rendered from here). Overwriting
# such a config with a single-leg render would SILENTLY DELETE the other cluster's leg: keepalived would
# reload happily, and the missing VIP would only be noticed when a failover didn't happen.
# Refuse rather than clobber; the operator can merge by hand and re-run with HA_FORCE_KEEPALIVED_CONF=1.
if [ -f /etc/keepalived/keepalived.conf ] && [ "${HA_FORCE_KEEPALIVED_CONF:-0}" != 1 ]; then
  _live_legs=$(grep -c '^[[:space:]]*vrrp_instance' /etc/keepalived/keepalived.conf || true)
  if [ "${_live_legs:-0}" -gt 1 ]; then
    echo "ERROR: /etc/keepalived/keepalived.conf has $_live_legs vrrp_instance blocks; this template renders 1."
    echo "       Overwriting would delete the other leg (e.g. .210's air-gap block). Merge by hand, or"
    echo "       re-run with HA_FORCE_KEEPALIVED_CONF=1 if you really mean to replace the whole file."
    echo "       A backup of the current file is at /etc/keepalived/keepalived.conf.bak-predeploy-*"
    sudo cp /etc/keepalived/keepalived.conf \
            "/etc/keepalived/keepalived.conf.bak-predeploy-$(date -u +%Y%m%dT%H%M%SZ)"
    exit 1
  fi
fi

sed -e "s/@STATE@/$STATE/" -e "s/@PRIORITY@/$PRIORITY/" -e "s#@IFACE@#$IFACE#" \
    -e "s#@VIP@#$VIP#" -e "s/@VRID@/$VRID/" -e "s/@CIDR@/$CIDR/" \
    "$REPO/failover/keepalived.conf.tmpl" | sudo tee /etc/keepalived/keepalived.conf >/dev/null
echo "    wrote /etc/keepalived/keepalived.conf"

# ha-controller unit must EXIST so notify.sh can start it on takeover. Install (disabled on standby).
if ! systemctl cat ha-controller >/dev/null 2>&1; then
  sudo cp "$REPO/systemd/ha-controller.service" /etc/systemd/system/ha-controller.service
  sudo systemctl daemon-reload
  echo "    installed ha-controller.service"
fi
# ADR-0033 part 2: fence-over-bus listener runs on BOTH roles (either box can be the fence TARGET). Deploys
# DRY-RUN by default (unit Environment=FENCE_DRY_RUN=1) — observes but never stops until the drill flips it.
sudo cp "$REPO/failover/systemd/ha-fence-listener.service" /etc/systemd/system/ha-fence-listener.service
sudo systemctl daemon-reload
echo "    installed ha-fence-listener.service (dry-run default; enable --now after fence key is present)"

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
echo "    sudo systemctl enable --now ha-fence-listener.service   # both roles (dry-run until drill flips FENCE_DRY_RUN=0)"
