# Bench self-test image for the hvac_c6 / dehum_c6 hardware (XIAO C6 + Waveshare TTL TO RS485 (C) + relay).
# NOT a node build: no WiFi, no MQTT, no NVS. Flash by cable, run tools/hvac_bench.py, then reflash the
# real node image. See README.md.
cmake_minimum_required(VERSION 3.16)
set(_shared "${CMAKE_CURRENT_LIST_DIR}/../../../firmware/components")
set(EXTRA_COMPONENT_DIRS "${_shared}/ha_rs485")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(ha-c6-hvac-bench)
