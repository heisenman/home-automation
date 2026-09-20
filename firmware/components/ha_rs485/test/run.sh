#!/usr/bin/env sh
# Host unit test for the ha_rs485 TIMING core — no ESP-IDF, just a C compiler.
# The transport half (ha_rs485.c) is UART glue and is only meaningful against hardware.
set -e
here="$(dirname "$0")"
cc -Wall -Wextra "$here/test_ha_rs485_timing.c" "$here/../ha_rs485_timing.c" \
   -I"$here/../include" -o "${TMPDIR:-/tmp}/ha_rs485_timing_test"
exec "${TMPDIR:-/tmp}/ha_rs485_timing_test"
