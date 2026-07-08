#!/usr/bin/env bash
# provisioning/airgap/bridge/install-bridge.sh — install/remove the .210 web bridge (nginx reverse proxy
# from the household side to ha-2's air-gap API). App-level gateway — preserves the air gap (ip_forward=0).
# Run ON .210. Usage: install-bridge.sh [--uninstall]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CONF=/etc/nginx/conf.d/ha-web-bridge.conf

if [ "${1:-}" = --uninstall ]; then
  sudo rm -f "$CONF"
  sudo nginx -t && sudo systemctl reload nginx
  echo "web bridge removed (nginx still running for anything else)"; exit 0
fi

command -v nginx >/dev/null 2>&1 || sudo apt-get install -y -q nginx
sudo rm -f /etc/nginx/sites-enabled/default        # we bind explicitly to the household IP
sudo cp "$HERE/ha-web-bridge.conf" "$CONF"
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
echo "web bridge installed + enabled: https://192.168.0.210/  ->  ha-2 (192.168.1.210:8123)"
echo "verify: curl -sk -o /dev/null -w '%{http_code}\\n' https://192.168.0.210/api/v1/sensors  (expect 200)"
