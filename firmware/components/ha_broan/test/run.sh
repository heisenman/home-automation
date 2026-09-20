#!/usr/bin/env sh
# Host unit test for the Broan frame codec — no ESP-IDF, just a C compiler.
set -e
here="$(dirname "$0")"
cc -Wall -Wextra "$here/test_ha_broan_frame.c" "$here/../ha_broan_frame.c" \
   -I"$here/../include" -o "${TMPDIR:-/tmp}/ha_broan_frame_test"
exec "${TMPDIR:-/tmp}/ha_broan_frame_test"
