#!/usr/bin/env sh
# Host unit test for ha_gaposa — no ESP-IDF, just a C compiler.
set -e
here="$(dirname "$0")"
cc -Wall -Wextra "$here/test_ha_gaposa.c" "$here/../ha_gaposa.c" \
   -I"$here/../include" -o "${TMPDIR:-/tmp}/ha_gaposa_test"
exec "${TMPDIR:-/tmp}/ha_gaposa_test"
