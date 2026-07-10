#!/usr/bin/env bash
# Home Automation — one-time bootstrap (requires sudo)
# Run from the repo root:  bash install.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$REPO_DIR/venv"
SYSTEMD_DEST="/etc/systemd/system"

echo "=== Home Automation install ==="
echo "Repo: $REPO_DIR"

# ── System packages ───────────────────────────────────────────────────────────
echo
echo "--- Installing system packages ---"
sudo apt-get update -q
sudo apt-get install -y \
    mosquitto \
    mosquitto-clients \
    python3-venv \
    python3-pip

# Add visko to bluetooth group (takes effect on next login / new shell)
sudo usermod -aG bluetooth visko
echo "visko added to bluetooth group (re-login to apply)"

# ── Python venv ───────────────────────────────────────────────────────────────
echo
echo "--- Creating Python venv at $VENV_DIR ---"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/server/requirements.txt"
echo "Python packages installed"

# ── Instance directories ──────────────────────────────────────────────────────
echo
echo "--- Creating instance directories ---"
mkdir -p \
    "$REPO_DIR/instance/db/parquet" \
    "$REPO_DIR/instance/mosquitto"

# Seed device registry from example if not present
if [[ ! -f "$REPO_DIR/instance/devices.yaml" ]]; then
    cp "$REPO_DIR/config-examples/devices.example.yaml" "$REPO_DIR/instance/devices.yaml"
    echo "Seeded instance/devices.yaml — EDIT this file with your real device MACs"
fi

# ── Mosquitto ─────────────────────────────────────────────────────────────────
echo
echo "--- Configuring Mosquitto ---"
# Stop the default mosquitto service if running (we'll use our config)
sudo systemctl stop mosquitto 2>/dev/null || true
sudo systemctl disable mosquitto 2>/dev/null || true

# Install the baseline (anonymous) drop-in ONLY on a fresh box. Do NOT clobber an existing
# homeauto.conf — once the broker auth/ACL cutover (provisioning/broker-auth-cutover.md) has installed
# the AUTHENTICATED config there, re-running install.sh must not silently revert it to anonymous.
if [[ ! -f /etc/mosquitto/conf.d/homeauto.conf ]]; then
    sudo cp "$REPO_DIR/server/config/mosquitto.conf" /etc/mosquitto/conf.d/homeauto.conf
    echo "Installed baseline mosquitto config (anonymous — run the auth cutover to harden)"
else
    echo "homeauto.conf already present — leaving it as-is (preserves broker auth if configured)"
fi

# Restart with our config
sudo systemctl enable mosquitto
sudo systemctl start mosquitto
echo "Mosquitto started"

# ── Systemd service units ─────────────────────────────────────────────────────
echo
echo "--- Installing systemd units ---"
# Install ALL committed unit files (not a hardcoded subset). A hardcoded list drifts: ha-levoit-bridge was
# omitted -> never installed on ha-2 -> the Levoit purifier went unmapped ~14h (see docs/REQUIRED-SERVICES.md).
# Installing a unit FILE is harmless (nothing runs until enabled); this eliminates the "unit not-found" class.
# Which units to ENABLE is role-specific (below + the required-services checklist). Air-gap units live in
# provisioning/airgap/ and are installed by their own scripts.
for unit_path in "$REPO_DIR"/systemd/*.service "$REPO_DIR"/systemd/*.timer; do
    unit="$(basename "$unit_path")"
    # Template the real repo path into each unit so the install isn't tied to a fixed location. The committed
    # units use /home/visko/home_automation as the default; this rewrites them to wherever the repo lives.
    sudo sed "s#/home/visko/home_automation#${REPO_DIR}#g" "$unit_path" \
        | sudo tee "$SYSTEMD_DEST/$unit" >/dev/null
done

sudo systemctl daemon-reload

# Enable + start this host's required units FROM THE MANIFEST (no hardcoded list — that drift is what left
# ha-levoit-bridge off ha-2 for ~14h). The set is provisioning/required-services.yaml resolved against this
# box's instance/host-roles.yaml. Units whose file isn't installed here (e.g. air-gap units, or a role this
# host doesn't hold) are skipped gracefully. See docs/REQUIRED-SERVICES.md.
if [ -f "$REPO_DIR/instance/host-roles.yaml" ]; then
    for u in $("$REPO_DIR/venv/bin/python3" "$REPO_DIR/tools/required_services.py" list --enable 2>/dev/null); do
        sudo systemctl enable --now "$u" 2>/dev/null && echo "  enabled $u" || echo "  (skip $u — not installed here / gated)"
    done
    echo "--- supervisor check (should report GAP=0) ---"
    "$REPO_DIR/venv/bin/python3" "$REPO_DIR/tools/required_services.py" check --no-alert || true
else
    echo "!! no instance/host-roles.yaml — create it (e.g. 'roles: [core, dictator]') then re-run install.sh"
    echo "   falling back to the minimal core so the box isn't dead:"
    sudo systemctl enable --now ha-writer.service ha-api.service
fi

# Edge history ingest — reassembles on-device GATT-pull history (home/edge/+/+/history) into hot.db.
# Idle (just a subscriber) until a pull runs; always-on so autonomous pulls Just Work.
sudo systemctl enable ha-edge-history.service
sudo systemctl start ha-edge-history.service

# Scanner needs bluetooth group — start it last
sudo systemctl enable ha-scanner.service
sudo systemctl start ha-scanner.service

echo
echo "=== Install complete ==="
echo
echo "Next steps:"
echo "  1. Edit instance/devices.yaml — add your real SwitchBot/Aranet MAC addresses"
echo "     (run: mosquitto_sub -h localhost -t 'home/unknown/#' -v  to find unknown MACs)"
echo "  2. Check logs:"
echo "       journalctl -u ha-scanner -f"
echo "       journalctl -u ha-writer -f"
echo "  3. Verify MQTT traffic:"
echo "       mosquitto_sub -h localhost -t 'home/#' -v"
echo "  4. API is at http://localhost:8123/docs"
echo
echo "To backfill from a SwitchBot app CSV export, run:"
echo "  venv/bin/python3 tools/import_switchbot_csv.py --help"
echo "(Direct BLE history sync — pulling each meter's 36-68 day on-device log —"
echo " is planned; see docs/adr/ADR-0007.)"
echo
echo "Internet weather recorder (outdoor comparison data):"
echo "  cp config-examples/weather.env.example instance/weather.env  # then set your lat/lon"
echo "  sudo systemctl enable --now ha-weather.timer                 # records every 15 min"
echo "  venv/bin/python3 -m server.weather --once --lat <LAT> --lon <LON>   # one-off test"
