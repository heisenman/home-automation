# Top-level ESP-IDF project file — IoTorero/Athom PG03V3-US16A smart plug (ESP32-C3), ADR-0042.
cmake_minimum_required(VERSION 3.16)
# Shared firmware components (ADR-0020), listed explicitly (see edge/esp32c6-dehum/CMakeLists.txt for why
# the dir is not globbed, and why the BLE components are linked but never called on a NO_BLE node).
set(_shared "${CMAKE_CURRENT_LIST_DIR}/../../firmware/components")
set(EXTRA_COMPONENT_DIRS
    "${_shared}/ha_config"
    "${_shared}/ha_mqtt"
    "${_shared}/ha_ota"
    "${_shared}/ha_sntp"
    "${_shared}/ha_wifi"
    "${_shared}/ha_cse7766"
    "${_shared}/ha_partmig"
    "${_shared}/ha_relay"
    "${_shared}/ha_reach"
    "${_shared}/switchbot_decode"
    "${_shared}/ha_gatt"
    "${_shared}/ha_gatt_exec"
    "${_shared}/ha_ble_scan")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(ha-edge-c3-plug)
