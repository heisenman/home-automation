#!/usr/bin/env sh
# Host unit test for ha_battery_profile's pure core — no ESP-IDF, just a C compiler.
set -e
here="$(dirname "$0")"
cc "$here/test_ha_battery_profile.c" "$here/../ha_battery_profile.c" \
   -I"$here/../include" -o "${TMPDIR:-/tmp}/ha_batt_profile_test"
exec "${TMPDIR:-/tmp}/ha_batt_profile_test"
