#!/usr/bin/env sh
# Host unit test for the c6-hvac telemetry JSON builder — no ESP-IDF, just a C compiler.
# The rest of this build is IDF glue and is only meaningful against hardware; the protocol itself is
# covered by firmware/components/{ha_rs485,ha_broan}/test/run.sh.
set -e
here="$(dirname "$0")"
cc -Wall -Wextra "$here/test_jsonbuf.c" "$here/../main/jsonbuf.c" \
   -I"$here/../main" -o "${TMPDIR:-/tmp}/c6_hvac_jsonbuf_test"
exec "${TMPDIR:-/tmp}/c6_hvac_jsonbuf_test"
