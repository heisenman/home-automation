#!/usr/bin/env bash
# Host test for the ha_rtc pure register core — no IDF, plain cc. (ADR-0020: pure modules ship a host test.)
set -e
cd "$(dirname "$0")"
cc -std=c11 -Wall -Wextra -Werror -D_GNU_SOURCE -DHA_RTC_HOST_TEST -I../include ../ha_rtc_regs.c test_ha_rtc.c -o /tmp/ha_rtc_test
/tmp/ha_rtc_test
