#!/usr/bin/env bash
# OFFLINE tool — build a SIGNED break-glass recovery bundle (ADR-0033 §5.1). Run on an admin machine that
# holds the recovery PRIVATE key (kept OFF the boxes). Writes recovery.tar + recovery.sig to a target dir
# (a mounted USB labelled HA-RECOVER, or a dir you then `cp` to such a USB).
#
# Usage:
#   make-usb.sh <priv-key.pem> <out-dir> [--ssh-key <authorized_key.pub>] [--actions <actions.sh>]
#
# The bundle may carry an admin SSH pubkey to re-add and/or a custom actions.sh — both are covered by the
# signature the box verifies before executing anything.
set -euo pipefail
PRIV="${1:?priv key}"; OUT="${2:?out dir}"; shift 2
SSHKEY=""; ACTIONS=""
while [ $# -gt 0 ]; do case "$1" in
  --ssh-key) SSHKEY="$2"; shift 2;;
  --actions) ACTIONS="$2"; shift 2;;
  *) echo "unknown arg $1"; exit 1;;
esac; done
[ -r "$PRIV" ] || { echo "priv key unreadable: $PRIV"; exit 1; }
mkdir -p "$OUT"
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
[ -n "$SSHKEY" ]  && install -m 0644 "$SSHKEY"  "$STAGE/authorized_key.pub"
[ -n "$ACTIONS" ] && install -m 0755 "$ACTIONS" "$STAGE/actions.sh"
# a manifest so the bundle is never empty (recovery always restarts services + restores LKG regardless)
date -Is > "$STAGE/created" 2>/dev/null || date > "$STAGE/created"
tar -cf "$OUT/recovery.tar" -C "$STAGE" .
openssl dgst -sha256 -sign "$PRIV" -out "$OUT/recovery.sig" "$OUT/recovery.tar"
echo "OK: wrote $OUT/recovery.tar + recovery.sig"
echo "   -> copy both to a USB labelled HA-RECOVER (e.g. mkfs.ext4 -L HA-RECOVER /dev/sdX1)"
