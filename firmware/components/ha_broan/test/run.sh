#!/usr/bin/env sh
# Host unit tests for ha_broan — no ESP-IDF, just a C compiler.
#   1. the pure wire codec (framing, checksum, register encode/decode)
#   2. the session state machine, driven against a simulated ERV
set -e
here="$(dirname "$0")"
out="${TMPDIR:-/tmp}"

cc -Wall -Wextra "$here/test_ha_broan_frame.c" "$here/../ha_broan_frame.c" \
   -I"$here/../include" -o "$out/ha_broan_frame_test"
"$out/ha_broan_frame_test"

echo
cc -Wall -Wextra "$here/test_ha_broan_sm.c" "$here/../ha_broan.c" "$here/../ha_broan_frame.c" \
   -I"$here/../include" -o "$out/ha_broan_sm_test"
exec "$out/ha_broan_sm_test"
