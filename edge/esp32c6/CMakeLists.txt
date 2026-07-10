# Top-level ESP-IDF project file.
cmake_minimum_required(VERSION 3.16)
# Shared firmware components (ADR-0020) live at repo-root firmware/components. List the ones THIS
# build consumes explicitly rather than globbing the whole dir — that dir also holds panel-only
# components (ha_battery/ha_sdcard/fs_ops) whose managed deps (esp_io_expander, sdmmc, ...) aren't
# available to an edge build, so a whole-dir glob fails IDF component resolution.
set(_shared "${CMAKE_CURRENT_LIST_DIR}/../../firmware/components")
set(EXTRA_COMPONENT_DIRS
    "${_shared}/switchbot_decode"
    "${_shared}/ha_config"
    "${_shared}/ha_mqtt"
    "${_shared}/ha_ble_scan"
    "${_shared}/ha_gatt"
    "${_shared}/ha_ota"
    "${_shared}/ha_gatt_exec"
    "${_shared}/ha_relay"
    "${_shared}/ha_sntp"
    "${_shared}/ha_reach"
    "${_shared}/ha_wifi"
    "${_shared}/sgp40"
    "${_shared}/sgp30"
    "${_shared}/sensirion_gas_index"
    "${_shared}/bme680")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(ha-edge-c6)
