#!/usr/bin/env bash
# Remaster the official Debian 13 netinst ISO into an UNATTENDED bootstrap ISO:
#   - preseed.cfg injected into the installer initrd (most reliable path)
#   - firstboot.sh copied to /ha/ on the ISO (preseed late_command installs it)
#   - boot menus default to a fully automated install
#
# Run on any Debian/Ubuntu workstation (NOT the target). Requires: xorriso, isolinux, cpio, gzip.
#   sudo apt install xorriso isolinux
#
# Usage:
#   ./build-seed-iso.sh  debian-13.x.0-amd64-netinst.iso  ha-bootstrap.iso
#
# Then flash:  sudo dd if=ha-bootstrap.iso of=/dev/sdX bs=4M status=progress conv=fsync
#
# REMINDER: fill the <PLACEHOLDER>s in ../autoinstall/preseed.cfg first (hostname, password
# hash, SSH key).  Verify none remain:  grep -n PLACEHOLDER ../autoinstall/preseed.cfg
set -euo pipefail

SRC_ISO="${1:?usage: build-seed-iso.sh <src-netinst.iso> <out.iso>}"
OUT_ISO="${2:?usage: build-seed-iso.sh <src-netinst.iso> <out.iso>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PRESEED="${HERE}/../autoinstall/preseed.cfg"
FIRSTBOOT="${HERE}/firstboot.sh"
WORK="$(mktemp -d)"; ISO="${WORK}/iso"
trap 'rm -rf "$WORK"' EXIT

if grep -q PLACEHOLDER "$PRESEED"; then
  echo "ERROR: unfilled <PLACEHOLDER> in $PRESEED — set hostname / password hash / SSH key first." >&2
  exit 1
fi

echo "==> extracting $SRC_ISO"
xorriso -osirrox on -indev "$SRC_ISO" -extract / "$ISO" >/dev/null 2>&1
chmod -R u+w "$ISO"

echo "==> injecting preseed.cfg into installer initrd"
# amd64 graphical+text installer initrd lives here on Debian netinst:
INITRD="$ISO/install.amd/initrd.gz"
[ -f "$INITRD" ] || { echo "ERROR: $INITRD not found (is this a Debian amd64 netinst?)"; exit 1; }
cp "$PRESEED" "$WORK/preseed.cfg"
gunzip "$INITRD"
( cd "$WORK" && echo "preseed.cfg" | cpio -H newc -o -A -F "$ISO/install.amd/initrd" ) >/dev/null 2>&1
gzip "$ISO/install.amd/initrd"

echo "==> copying firstboot.sh to /ha on the ISO"
mkdir -p "$ISO/ha"; cp "$FIRSTBOOT" "$ISO/ha/firstboot.sh"

echo "==> wiring boot menus for an automated install"
# priority=high (not critical): everything we preseed still auto-answers, but genuinely-unset
# high-priority prompts — i.e. the Wi-Fi ESSID/passphrase on wired-failure fallback — are shown
# to the user instead of being skipped. Keep it 'high' for the wired-first/Wi-Fi-fallback flow.
AUTO='auto=true priority=high preseed/file=/preseed.cfg ---'
# BIOS (isolinux)
if [ -f "$ISO/isolinux/txt.cfg" ]; then
  cat > "$ISO/isolinux/txt.cfg" <<EOF
default autoinstall
label autoinstall
  menu label ^Automated HA install
  kernel /install.amd/vmlinuz
  append vga=788 initrd=/install.amd/initrd.gz $AUTO
EOF
  sed -i 's/^timeout .*/timeout 10/' "$ISO/isolinux/isolinux.cfg" 2>/dev/null || true
fi
# UEFI (grub)
if [ -f "$ISO/boot/grub/grub.cfg" ]; then
  cat > "$ISO/boot/grub/grub.cfg" <<EOF
set default=0
set timeout=1
menuentry "Automated HA install" {
  linux /install.amd/vmlinuz $AUTO
  initrd /install.amd/initrd.gz
}
EOF
fi

echo "==> repacking $OUT_ISO (BIOS + UEFI hybrid)"
xorriso -as mkisofs -r -V 'HA_BOOTSTRAP' -o "$OUT_ISO" \
  -J -joliet-long \
  -isohybrid-mbr /usr/lib/ISOLINUX/isohdpfx.bin \
  -c isolinux/boot.cat -b isolinux/isolinux.bin \
    -no-emul-boot -boot-load-size 4 -boot-info-table \
  -eltorito-alt-boot -e boot/grub/efi.img -no-emul-boot -isohybrid-gpt-basdat \
  "$ISO"

echo "==> done: $OUT_ISO"
echo "    Flash:  sudo dd if=$OUT_ISO of=/dev/sdX bs=4M status=progress conv=fsync"
