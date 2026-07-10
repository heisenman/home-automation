#!/usr/bin/env bash
# Install the break-glass USB recovery responder (ADR-0033 §5.1) on a box.
#   - installs the trusted recovery PUBLIC key (verification anchor)
#   - installs breakglass-recover.sh + the udev rule + the ha-break-glass@ service
# On first run (no keypair given) it GENERATES an EC recovery keypair and prints where the PRIVATE key is —
# MOVE THAT OFF THE BOX and keep it offline (it is the authority to recover any box).
#
# Usage:  sudo install-break-glass.sh [recovery.pub]
#   recovery.pub given -> install it as the trusted key (normal case on additional boxes)
#   omitted           -> generate a fresh keypair here (first box / key ceremony)
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ETC=/etc/ha-break-glass
install -d -m 0755 "$ETC"

if [ -n "${1:-}" ]; then
  install -m 0644 "$1" "$ETC/recovery.pub"
  echo "installed trusted pubkey from $1"
elif [ ! -f "$ETC/recovery.pub" ]; then
  echo "== generating EC recovery keypair (key ceremony) =="
  PRIV="$ETC/recovery.key.MOVE-OFFLINE.pem"
  openssl ecparam -name prime256v1 -genkey -noout -out "$PRIV"
  openssl ec -in "$PRIV" -pubout -out "$ETC/recovery.pub" 2>/dev/null
  chmod 0600 "$PRIV"
  echo "!! PRIVATE KEY written to $PRIV — MOVE IT OFF THIS BOX now and delete the local copy."
  echo "   (it signs recovery USBs via make-usb.sh; the box only keeps recovery.pub)"
fi

install -m 0755 "$HERE/breakglass-recover.sh" /usr/local/sbin/breakglass-recover.sh
install -m 0644 "$HERE/systemd/ha-break-glass@.service" /etc/systemd/system/ha-break-glass@.service
install -m 0644 "$HERE/99-ha-break-glass.rules" /etc/udev/rules.d/99-ha-break-glass.rules
systemctl daemon-reload
udevadm control --reload-rules 2>/dev/null || true

# Snapshot the current firewall as last-known-good so recovery can restore it (placeholder until Phase 1c
# writes the real ruleset; harmless if nft has no ruleset yet).
nft list ruleset > "$ETC/nftables-lastknowngood.nft" 2>/dev/null || true

echo "OK: break-glass responder installed. Trigger: insert a USB labelled HA-RECOVER with a signed bundle"
echo "    (build one offline with make-usb.sh <priv> <usb-mount> [--ssh-key key.pub] [--actions actions.sh])."
