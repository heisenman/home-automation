#!/usr/bin/env sh
# Host unit test for ha_cse7766 (pure C, no ESP-IDF).
set -e
here="$(dirname "$0")"
cc -Wall -Wextra "$here/test_ha_cse7766.c" "$here/../ha_cse7766.c" \
   -I"$here/../include" -o "${TMPDIR:-/tmp}/ha_cse7766_test" -lm
exec "${TMPDIR:-/tmp}/ha_cse7766_test"
