#!/usr/bin/env sh
# Host unit test for ha_power_policy's decision core — no ESP-IDF, just a C compiler.
# Compiles ONLY ha_power_policy.c (the pure core); the runtime (ha_power_policy_rt.c) needs FreeRTOS.
set -e
here="$(dirname "$0")"
cc "$here/test_ha_power_policy.c" "$here/../ha_power_policy.c" \
   -I"$here/../include" -o "${TMPDIR:-/tmp}/ha_pwr_policy_test"
exec "${TMPDIR:-/tmp}/ha_pwr_policy_test"
