#!/bin/bash
# install-unit — bounded, sudo-NOPASSWD helper for peer-repair (plan crystalline-spinning-spindle, Layer 2).
#
# The healer's peer-repair recovers a MISSING unit file by pulling the canonical copy from the cluster peer
# (over the reconcile-agent allowlist) into /tmp, then calls THIS to install it. Copying into
# /etc/systemd/system needs root; rather than widen sudo to a bare `cp` (arbitrary-write), this helper is the
# ONLY privileged step and it is tightly bounded:
#   - the unit NAME must be charset-clean (no path chars / traversal) and end .service|.timer
#   - the unit must be one this host actually requires (present in `required_services.py list`) — you can only
#     (re)install a KNOWN required unit, never an arbitrary unit
#   - the staged file at /tmp/<name> must exist and look like a systemd unit
# Then: copy to /etc/systemd/system/<name> + daemon-reload. Nothing else. Wire via sudoers NOPASSWD.
#
# Usage (under sudo):  sudo install-unit.sh <unit-name>
set -u
REPO=/home/visko/home_automation
name="${1:-}"
staged="/tmp/${name}"
target="/etc/systemd/system/${name}"

die(){ echo "install-unit: $1" >&2; exit 1; }

# 1. name charset + suffix (blocks path traversal, metacharacters, absolute paths)
[[ "$name" =~ ^[A-Za-z0-9._-]+\.(service|timer)$ ]] || die "bad unit name '$name'"

# 2. must be a required unit for THIS host (manifest-bounded — not an arbitrary unit)
if ! /home/visko/home_automation/venv/bin/python3 "$REPO/tools/required_services.py" list 2>/dev/null \
      | grep -qxF "$name"; then
  die "'$name' is not a required unit for this host — refusing"
fi

# 3. staged file exists and looks like a unit
[ -f "$staged" ] || die "no staged file at $staged"
grep -q '^\[Unit\]' "$staged" || die "$staged does not look like a systemd unit"

install -m 0644 -o root -g root "$staged" "$target" || die "copy to $target failed"
systemctl daemon-reload || die "daemon-reload failed"
echo "install-unit: installed $target + daemon-reload"
