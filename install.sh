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
    python3.12-venv \
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

# Install our config as a drop-in
sudo cp "$REPO_DIR/server/config/mosquitto.conf" /etc/mosquitto/conf.d/homeauto.conf

# Restart with our config
sudo systemctl enable mosquitto
sudo systemctl start mosquitto
echo "Mosquitto started"

# ── Systemd service units ─────────────────────────────────────────────────────
echo
echo "--- Installing systemd units ---"
for unit in ha-scanner.service ha-writer.service ha-api.service \
            ha-compactor.service ha-compactor.timer \
            ha-verify-hashes.service ha-verify-hashes.timer; do
    sudo cp "$REPO_DIR/systemd/$unit" "$SYSTEMD_DEST/$unit"
done

sudo systemctl daemon-reload

# Enable and start services
sudo systemctl enable ha-writer.service ha-api.service
sudo systemctl enable ha-compactor.timer ha-verify-hashes.timer
sudo systemctl start ha-writer.service ha-api.service
sudo systemctl start ha-compactor.timer ha-verify-hashes.timer

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
echo "For SwitchBot history import, run:"
echo "  venv/bin/python3 tools/import_switchbot_history.py --help"
