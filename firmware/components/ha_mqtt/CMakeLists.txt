# ha_mqtt: shared edge MQTT control-plane + publish paths (ADR-0010/0023). Promoted from
# edge/esp32c6/main/ha_mqtt.c; board bits (per-node secrets, LED hooks, reach flag) are the ha_mqtt_cfg_t
# seam (ha_mqtt_init), so this component is secrets-free. It orchestrates the other shared components
# (ha_gatt/ha_gatt_exec/ha_ota/ha_relay/ha_reach/ha_ble_scan/ha_sntp) and wires their platform seams here.
idf_component_register(SRCS "ha_mqtt.c"
                       INCLUDE_DIRS "include"
                       REQUIRES mqtt mbedtls nvs_flash app_update json
                                switchbot_decode ha_sntp ha_gatt ha_gatt_exec ha_ota ha_relay ha_reach ha_ble_scan ha_config)

# CONFIG_HA_MQTT_NO_BLE (Kconfig, default n) — for a node that drives contacts rather than scanning
# (ADR-0041's ha-shades / ha-hvac). It compiles out the BLE-only ops (history / gatt / batt_refresh) and,
# critically, NULLs the OTA radio-pause seam: ha_ble_scan_resume() calls start_scan(), so on a node that
# never initialised NimBLE the OTA path would otherwise drive an uninitialised stack.
#
# ⚠️ REQUIRES above stays UNCONDITIONAL on purpose. IDF expands component requirements (build.cmake:686)
# BEFORE it generates sdkconfig (build.cmake:705), in a child `cmake -P` that sees neither CONFIG_ symbols
# nor project-scope variables — so a conditional REQUIRES silently fails to resolve. A NO_BLE consumer
# therefore still lists ha_gatt/ha_gatt_exec/ha_ble_scan in EXTRA_COMPONENT_DIRS and enables CONFIG_BT so
# they compile; they are simply linked-and-never-called. target_compile_definitions runs after Kconfig, so
# it CAN be conditional.
if(CONFIG_HA_MQTT_NO_BLE)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE CONFIG_HA_MQTT_NO_BLE=1)
endif()
