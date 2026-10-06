#!/usr/bin/env sh
# Host unit test for ha_aprilaire (pure C, no ESP-IDF). Frames are live captures from our E070.
set -e
here="$(dirname "$0")"
cc -Wall -Wextra "$here/test_ha_aprilaire.c" "$here/../ha_aprilaire.c" \
   -I"$here/../include" -o "${TMPDIR:-/tmp}/ha_aprilaire_test"
exec "${TMPDIR:-/tmp}/ha_aprilaire_test"
