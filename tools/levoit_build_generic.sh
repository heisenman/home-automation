#!/usr/bin/env bash
# Build the GENERIC Levoit Vital 200S ESPHome image that the PWA "Flash new hardware → Levoit Vital 200S" kind
# writes (server/maintenance/levoit_flash.py). Run on .210 (where provisioning/levoit/secrets.yaml lives), once,
# and again after any change to levoit-vital200s-c3.common.yaml / levoit-generic.yaml.
# Design: docs/design/pwa-levoit-flashing.md.
#
# Writes levoit-generic.build.json beside the image ({sha256, built, esphome}); the flasher refuses an image
# whose bytes don't match it, so a half-finished rebuild can never be flashed.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LEV="$REPO/provisioning/levoit"
OUT="$LEV/.esphome/build/levoit/.pioenvs/levoit"
IMAGE_REF="ghcr.io/esphome/esphome"

[ -f "$LEV/secrets.yaml" ] || { echo "no $LEV/secrets.yaml — build on .210 (see secrets.example.yaml)" >&2; exit 1; }
grep -q '^ota_password_generic:' "$LEV/secrets.yaml" \
  || { echo "secrets.yaml lacks ota_password_generic (openssl rand -hex 16)" >&2; exit 1; }
if ! docker info >/dev/null 2>&1; then
  echo "docker is not running (not enabled at boot on .210): sudo systemctl start docker" >&2; exit 1
fi

rm -f "$OUT/levoit-generic.build.json"     # the old record must not vouch for the new bytes mid-build
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$LEV":/config "$IMAGE_REF" compile levoit-generic.yaml

ver="$(docker run --rm "$IMAGE_REF" version 2>/dev/null | grep -oE '[0-9]{4}\.[0-9]+\.[0-9]+' | head -1)"
sha="$(sha256sum "$OUT/firmware.factory.bin" | awk '{print $1}')"
printf '{"sha256": "%s", "built": "%s", "esphome": "%s", "config": "provisioning/levoit/levoit-generic.yaml"}\n' \
  "$sha" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$ver" > "$OUT/levoit-generic.build.json"
echo "generic Levoit image ready: $OUT/firmware.factory.bin sha256=${sha:0:16} esphome=$ver"
