#!/usr/bin/env bash
# install-reconcile-lock — lock the dedicated failover port (47222) to reconcile-only (ADR-0033 part2 item2).
#
# Wires `Match LocalPort 47222 / ForceCommand <reconcile-agent.sh>` so the one inbound SSH port left open on
# the air-gap boundary is NOT a full shell + sudo via the shared id_cluster key — only the reconcile/sync verbs.
# Port 22 stays a full shell (management escape hatch). Idempotent. Run on EACH box on the 47222 line (.210 + ha-2).
#
# The Match is appended to the END of the MAIN sshd_config (NOT a sshd_config.d/ drop-in): the Debian Include
# sits near the top with globals (e.g. `Subsystem sftp`) after it, so a Match in a .d file would wrongly shadow
# those globals. A terminal Match at EOF is the only ordering-safe placement.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
AGENT="$REPO/failover/cluster-ssh/reconcile-agent.sh"
CFG=/etc/ssh/sshd_config
MARK="part2 item2"

[ -x "$AGENT" ] || { chmod +x "$AGENT" 2>/dev/null || { echo "ERROR: $AGENT missing/not executable"; exit 1; }; }

if grep -q "$MARK" "$CFG"; then
  echo "reconcile-lock already present in $CFG — nothing to do"
else
  sudo cp "$CFG" "$CFG.bak.$(date +%s)"
  sudo tee -a "$CFG" >/dev/null <<EOF

# ADR-0033 $MARK: lock the dedicated failover port (47222) to reconcile-only via a ForceCommand wrapper.
# TERMINAL Match appended at EOF so no global directive falls under it. Port 22 stays full-shell (mgmt).
Match LocalPort 47222
    ForceCommand $AGENT
    PermitTTY no
    AllowTcpForwarding no
    X11Forwarding no
    AllowAgentForwarding no
EOF
  echo "appended terminal Match to $CFG"
fi

sudo sshd -t
sudo systemctl reload ssh 2>/dev/null || sudo systemctl reload sshd
echo "OK: 47222 is reconcile-only; :22 unchanged. Verify: ssh -p47222 <box> id  -> refused."
