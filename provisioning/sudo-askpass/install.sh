#!/usr/bin/env bash
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
H="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install -m 0555 "$H/ha-sudo-askpass" /usr/local/sbin/ha-sudo-askpass
install -m 0755 "$H/sudo-wrapper"    /usr/local/bin/sudo
grep -q '^Path askpass' /etc/sudo.conf 2>/dev/null || echo 'Path askpass /usr/local/sbin/ha-sudo-askpass' >> /etc/sudo.conf
echo "OK: sudo askpass wired. Test: setsid sh -c 'sudo true' </dev/null && echo works"
