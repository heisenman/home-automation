# Top-level ESP-IDF project file — XIAO ESP32-C6 dehumidifier node (ADR-0041).
cmake_minimum_required(VERSION 3.16)
# Shared firmware components (ADR-0020) live at repo-root firmware/components. Listed explicitly rather
# than globbing the dir — it also holds panel-only components (ha_battery/ha_sdcard/fs_ops) whose managed
# deps aren't available to an edge build, so a whole-dir glob fails IDF component resolution.
set(_shared "${CMAKE_CURRENT_LIST_DIR}/../../firmware/components")
set(EXTRA_COMPONENT_DIRS
    "${_shared}/ha_config"
    "${_shared}/ha_mqtt"
    "${_shared}/ha_ota"
    "${_shared}/ha_sntp"
    "${_shared}/ha_wifi"
    "${_shared}/ha_relay"
    "${_shared}/ha_reach"
    "${_shared}/switchbot_decode"
    # Linked but NEVER CALLED: CONFIG_HA_MQTT_NO_BLE compiles every use out of ha_mqtt, including the OTA
    # radio hooks (ha_ble_scan_resume() would otherwise drive an uninitialised NimBLE stack on this node).
    # They appear here only because IDF expands REQUIRES before sdkconfig exists, so ha_mqtt's dependency
    # list cannot be made conditional. See firmware/components/ha_mqtt/CMakeLists.txt.
    "${_shared}/ha_gatt"
    "${_shared}/ha_gatt_exec"
    "${_shared}/ha_ble_scan")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(ha-edge-c6-dehum)
