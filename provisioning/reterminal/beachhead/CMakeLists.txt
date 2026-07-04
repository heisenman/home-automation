# D1001 beachhead — prove ESP32-P4 + C6(esp-hosted) WiFi -> MQTT(.210) -> OTA
# before any UI. Connectivity-first (beachhead strategy, ADR-0019 Phase 1).
cmake_minimum_required(VERSION 3.16)
# Shared firmware modules (ha_battery, ha_battery_profile, switchbot_decode, fs_ops, …) live in the repo's
# firmware/components tree, reused across devices. Wire them in relative to this project so the build is
# self-contained on any checkout (bench or dev box) rather than depending on an ambient EXTRA_COMPONENT_DIRS
# env var — whose absence on a fresh shell is exactly what broke the resolve. Any env value still merges in.
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../../../firmware/components" ${EXTRA_COMPONENT_DIRS})
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(d1001_beachhead)
