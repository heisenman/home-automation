#!/usr/bin/env bash
# Apply the air-gap default-deny firewall (ADR-0033 §5) SAFELY — with a systemd-timed auto-rollback so a
# rule that isolates the box reverts on its own, and the break-glass USB (§5.1) is the deeper net.
#
#   apply-airgap-firewall.sh apply    # load ruleset + arm a 180s auto-rollback (removes table inet airgap)
#   apply-airgap-firewall.sh commit   # cancel the rollback + persist for boot
#   apply-airgap-firewall.sh rollback # remove the table now
#
# Rollback is clean because all changes live in ONE table (inet airgap); removing it restores prior behavior
# without touching docker/household rules.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)"; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RULES="$HERE/airgap.nft"
UNIT=airgap-fw-rollback
PERSIST=/etc/ha-airgap/airgap.nft
LKG=/etc/ha-break-glass/nftables-lastknowngood.nft

case "${1:-}" in
  apply)
    nft -c -f "$RULES"   # syntax check first
    # arm the backstop BEFORE applying, so even if apply wedges the link it reverts.
    systemctl stop "${UNIT}.timer" 2>/dev/null || true
    systemd-run --unit="$UNIT" --on-active=180 \
        /usr/sbin/nft delete table inet airgap >/dev/null 2>&1 || true
    nft -f "$RULES"
    echo "APPLIED (table inet airgap). Auto-rollback in 180s unless you run: $0 commit"
    ;;
  commit)
    systemctl stop "${UNIT}.timer" 2>/dev/null || true
    systemctl reset-failed "$UNIT" "${UNIT}.timer" 2>/dev/null || true
    install -d -m 0755 /etc/ha-airgap
    { echo "#!/usr/sbin/nft -f"; nft list table inet airgap; } > "$PERSIST"
    chmod 0644 "$PERSIST"
    install -m 0644 "$HERE/systemd/ha-airgap-firewall.service" /etc/systemd/system/ha-airgap-firewall.service
    systemctl daemon-reload; systemctl enable ha-airgap-firewall.service >/dev/null 2>&1 || true
    # refresh break-glass last-known-good to this GOOD ruleset
    [ -d /etc/ha-break-glass ] && cp "$PERSIST" "$LKG" 2>/dev/null || true
    echo "COMMITTED + persisted ($PERSIST, boot unit enabled, break-glass LKG refreshed)."
    ;;
  rollback)
    systemctl stop "${UNIT}.timer" 2>/dev/null || true
    nft delete table inet airgap 2>/dev/null || true
    echo "ROLLED BACK (table inet airgap removed)."
    ;;
  *) echo "usage: $0 {apply|commit|rollback}"; exit 1;;
esac
