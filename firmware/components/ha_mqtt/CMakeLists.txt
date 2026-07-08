# ha_mqtt: shared edge MQTT control-plane + publish paths (ADR-0010/0023). Promoted from
# edge/esp32c6/main/ha_mqtt.c; board bits (per-node secrets, LED hooks, reach flag) are the ha_mqtt_cfg_t
# seam (ha_mqtt_init), so this component is secrets-free. It orchestrates the other shared components
# (ha_gatt/ha_gatt_exec/ha_ota/ha_relay/ha_reach/ha_ble_scan/ha_sntp) and wires their platform seams here.
idf_component_register(SRCS "ha_mqtt.c"
                       INCLUDE_DIRS "include"
                       REQUIRES mqtt mbedtls nvs_flash app_update json
                                switchbot_decode ha_sntp ha_gatt ha_gatt_exec ha_ota ha_relay ha_reach ha_ble_scan ha_config)
