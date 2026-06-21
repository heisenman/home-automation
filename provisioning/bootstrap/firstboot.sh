#!/usr/bin/env bash
# HA first-boot provisioner — runs ONCE on first boot (online), then disables itself.
# Goal: leave the box with Node + Claude Code + the repo cloned, so an LLM can take over
# and execute provisioning/02-full-server-spec.md on-device.
#
# Idempotent: safe to re-run. Logs to /var/log/ha-firstboot.log (via the systemd unit).
set -euo pipefail

USER_NAME=visko
USER_HOME=/home/${USER_NAME}
REPO_URL="https://github.com/heisenman/home-automation.git"   # set to your fork/remote
REPO_DIR="${USER_HOME}/home_automation"
NODE_MAJOR=22                                                 # Claude Code needs Node >= 18

log(){ echo "[ha-firstboot $(date -u +%H:%M:%S)] $*"; }

log "waiting for network/DNS..."
for i in $(seq 1 30); do getent hosts github.com >/dev/null 2>&1 && break; sleep 2; done

log "apt: base toolchain"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y curl git ca-certificates build-essential python3-venv python3-dev pkg-config

# ── Node.js (NodeSource LTS) — needed to run Claude Code ───────────────────────────
if ! command -v node >/dev/null 2>&1 || [ "$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)" -lt 18 ]; then
  log "installing Node ${NODE_MAJOR}.x via NodeSource"
  curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
  apt-get install -y nodejs
fi
log "node $(node --version), npm $(npm --version)"

# ── Claude Code ────────────────────────────────────────────────────────────────────
# npm global install is the most portable; the native installer (claude.ai/install.sh)
# is an alternative. Authenticate after first SSH login:  `claude`  (OAuth) or export
# ANTHROPIC_API_KEY in the user's shell rc.
if ! command -v claude >/dev/null 2>&1; then
  log "installing Claude Code (npm global)"
  npm install -g @anthropic-ai/claude-code || log "WARN: claude install failed — install manually after login"
fi

# ── Clone the repo (so the spec + tooling are on-device) ───────────────────────────
if [ ! -d "${REPO_DIR}/.git" ]; then
  log "cloning ${REPO_URL}"
  sudo -u "${USER_NAME}" git clone "${REPO_URL}" "${REPO_DIR}" \
    || log "WARN: clone failed (private repo? provide a PAT/deploy key, then clone manually)"
fi

# ── MOTD: tell the human/LLM what to do next ───────────────────────────────────────
cat > /etc/motd <<'MOTD'
────────────────────────────────────────────────────────────────────
 Home Automation server — bootstrap complete.
 NEXT: run an LLM on-device and have it execute the full spec:
     cd ~/home_automation
     claude            # authenticate if prompted
     # then point it at: provisioning/02-full-server-spec.md
 Or drive remotely from your workstation via VSCode Remote-SSH.
 First-boot log: /var/log/ha-firstboot.log
────────────────────────────────────────────────────────────────────
MOTD

log "disabling self (one-shot complete)"
systemctl disable ha-firstboot.service || true
log "DONE."
