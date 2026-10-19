#!/usr/bin/env sh
# Host unit test for the ha_partmig planner (pure C, no ESP-IDF).
set -e
here="$(dirname "$0")"
cc -Wall -Wextra "$here/test_ha_partmig.c" "$here/../ha_partmig_plan.c" \
   -I"$here/../include" -o "${TMPDIR:-/tmp}/ha_partmig_test"
exec "${TMPDIR:-/tmp}/ha_partmig_test"
