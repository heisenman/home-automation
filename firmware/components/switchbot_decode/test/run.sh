#!/usr/bin/env sh
# Host unit test for switchbot_decode — no ESP-IDF, just a C compiler.
set -e
here="$(dirname "$0")"
cc "$here/test_switchbot_decode.c" "$here/../switchbot_decode.c" \
   -I"$here/../include" -lm -o "${TMPDIR:-/tmp}/sb_decode_test"
exec "${TMPDIR:-/tmp}/sb_decode_test"
