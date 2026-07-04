#!/usr/bin/env bash
# Host test for the ha_imu pure conversions — no IDF, plain cc. (ADR-0020: pure modules ship a host test.)
set -e
cd "$(dirname "$0")"
cc -std=c11 -Wall -Wextra -Werror -DHA_IMU_HOST_TEST -I../include ../ha_imu_regs.c test_ha_imu.c -o /tmp/ha_imu_test
/tmp/ha_imu_test
