#!/usr/bin/env sh
# Host unit test for ha_dout — no ESP-IDF, just a C compiler.
set -e
here="$(dirname "$0")"
cc -Wall -Wextra "$here/test_ha_dout.c" "$here/../ha_dout.c" \
   -I"$here/../include" -o "${TMPDIR:-/tmp}/ha_dout_test"
exec "${TMPDIR:-/tmp}/ha_dout_test"
