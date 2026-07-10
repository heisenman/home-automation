#!/usr/bin/env bash
# ha break-glass — on-box recovery from a SIGNED USB (ADR-0033 §5.1).
#
# Triggered by udev when a volume labelled HA-RECOVER appears. Verifies the bundle's signature against the
# box's trusted public key BEFORE running anything (an unsigned / wrong-signed / dropped USB is ignored —
# the signature is the security control; physical access alone is not enough). On a valid bundle it runs a
# fixed set of idempotent recovery actions to "fix a broken connection", then optionally the bundle's own
# actions.sh (also covered by the same signature).
#
# Usage (udev/service): breakglass-recover.sh /dev/sdX1
set -euo pipefail
DEV="${1:?usage: breakglass-recover.sh /dev/sdXN}"
PUB=/etc/ha-break-glass/recovery.pub          # trusted public key (baked in at install)
LKG=/etc/ha-break-glass/nftables-lastknowngood.nft
LOG=/var/log/ha-break-glass.log
log(){ echo "$(date -Is 2>/dev/null || date) breakglass: $*" | tee -a "$LOG" >&2; }

[ -r "$PUB" ] || { log "no trusted pubkey at $PUB — refusing"; exit 1; }
MNT="$(mktemp -d)"; trap 'umount "$MNT" 2>/dev/null || true; rmdir "$MNT" 2>/dev/null || true' EXIT
mount -o ro,noexec,nosuid,nodev "$DEV" "$MNT" 2>/dev/null || { log "mount $DEV failed"; exit 1; }

BUNDLE="$MNT/recovery.tar"; SIG="$MNT/recovery.sig"
[ -f "$BUNDLE" ] && [ -f "$SIG" ] || { log "no recovery.tar/.sig on $DEV — not a recovery volume"; exit 0; }

# ---- THE control: verify signature before touching anything ----
if ! openssl dgst -sha256 -verify "$PUB" -signature "$SIG" "$BUNDLE" >/dev/null 2>&1; then
  log "SIGNATURE INVALID on $DEV — ignoring (possible planted/dropped USB)"; exit 1
fi
log "signature OK — applying recovery"

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"; umount "$MNT" 2>/dev/null || true; rmdir "$MNT" 2>/dev/null || true' EXIT
tar -xf "$BUNDLE" -C "$WORK"

# ---- fixed, idempotent recovery actions ----
# 1. restore the firewall to last-known-good (undoes a bad nftables rule that isolated the box)
if [ -r "$LKG" ]; then nft -f "$LKG" && log "restored firewall from LKG" || log "nft restore FAILED"; fi
# 2. restart the crossing services
for u in ha-relay-broker ha-relay chrony mosquitto; do
  systemctl restart "$u" 2>/dev/null && log "restarted $u" || true
done
# 3. re-add an admin SSH key if the bundle carries one (re-open a locked-out admin path)
if [ -f "$WORK/authorized_key.pub" ]; then
  for home in /home/visko /root; do
    [ -d "$home" ] || continue
    install -d -m 700 "$home/.ssh"; touch "$home/.ssh/authorized_keys"
    grep -qxF "$(cat "$WORK/authorized_key.pub")" "$home/.ssh/authorized_keys" \
      || cat "$WORK/authorized_key.pub" >> "$home/.ssh/authorized_keys"
    log "ensured admin key in $home/.ssh/authorized_keys"
  done
fi
# 4. optional bundle-provided actions (signed) — full custom recovery
if [ -x "$WORK/actions.sh" ]; then log "running bundle actions.sh"; ( cd "$WORK" && ./actions.sh ) && log "actions.sh ok" || log "actions.sh FAILED"; fi

log "recovery complete"
