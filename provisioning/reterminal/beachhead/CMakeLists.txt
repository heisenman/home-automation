# D1001 beachhead — prove ESP32-P4 + C6(esp-hosted) WiFi -> MQTT(.210) -> OTA
# before any UI. Connectivity-first (beachhead strategy, ADR-0019 Phase 1).
cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(d1001_beachhead)
