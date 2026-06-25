#!/usr/bin/env bash
# Pre-stage the OpenWRT image for the incoming Netgear R7800 (board `openwrt-prestage`, theme B).
# Downloads the PINNED factory + sysupgrade images to a gitignored staging dir and verifies SHA256.
# The images are binaries — they are NEVER committed; this script reproduces them on demand.
#
#   ./provisioning/openwrt/fetch-image.sh            # -> instance/openwrt/<files>
#   OPENWRT_VER=25.12.4 ./provisioning/openwrt/fetch-image.sh
#
# Hardware CONFIRMED by Hugh 2026-06-25: Netgear R7800 = Qualcomm ipq806x, profile `netgear_r7800`.
set -euo pipefail

VER="${OPENWRT_VER:-25.12.4}"           # latest stable as of 2026-05 (bump + refresh checksums to re-pin)
TARGET="ipq806x/generic"
PROFILE="netgear_r7800"
BASE="https://downloads.openwrt.org/releases/${VER}/targets/${TARGET}"
OUT="${OPENWRT_STAGE:-instance/openwrt}"

# Pinned SHA256 for 25.12.4 (verified from the release sha256sums, 2026-06-24). If you bump OPENWRT_VER,
# replace these from: curl -s "$BASE/sha256sums" | grep r7800
FACTORY="openwrt-${VER}-ipq806x-generic-${PROFILE}-squashfs-factory.img"
SYSUP="openwrt-${VER}-ipq806x-generic-${PROFILE}-squashfs-sysupgrade.bin"
declare -A SHA=(
  ["$FACTORY"]="08a3cec5cc4b0db46d94abd82c1b49bbcd0bc84256a7243a064f0dbf4dc5ee74"
  ["$SYSUP"]="db791a5d9e5b16a78bd6a96969cf3923908a658eca431751fed8ab5258cf73cf"
)

mkdir -p "$OUT"
for f in "$FACTORY" "$SYSUP"; do
  dst="$OUT/$f"
  if [ ! -f "$dst" ]; then
    echo "↓ $f"
    curl -fSL --retry 3 -o "$dst" "$BASE/$f"
  fi
  want="${SHA[$f]}"
  got="$(sha256sum "$dst" | awk '{print $1}')"
  if [ "$want" != "$got" ]; then
    echo "✗ SHA256 MISMATCH for $f" >&2
    echo "  expected $want" >&2
    echo "  got      $got" >&2
    echo "  (refusing — do NOT flash this image)" >&2
    exit 1
  fi
  echo "✓ $f  sha256 OK"
done
echo
echo "Staged in $OUT/ :"
echo "  factory   (flash from Netgear stock firmware / TFTP recovery): $FACTORY"
echo "  sysupgrade (flash from a running OpenWRT, keeps settings):      $SYSUP"
echo "Next: see docs/openwrt-prestage.md for the flash + config runbook."
