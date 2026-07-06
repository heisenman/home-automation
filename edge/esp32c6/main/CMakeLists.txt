idf_component_register(
    SRCS
        "app_main.c"
        "ha_wifi.c"
        "ha_mqtt.c"
        "ha_gas.c"
    INCLUDE_DIRS "."
    REQUIRES nvs_flash esp_wifi esp_event esp_netif mqtt bt json
             app_update esp_https_ota esp_http_client mbedtls
             switchbot_decode ha_ble_scan ha_config ha_gatt ha_gatt_exec ha_ota ha_relay ha_sntp ha_reach
             sgp40 sgp30 sensirion_gas_index
)
