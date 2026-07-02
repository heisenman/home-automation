idf_component_register(
    SRCS
        "app_main.c"
        "ha_config.c"
        "ha_wifi.c"
        "ha_sntp.c"
        "ha_mqtt.c"
        "gatt_history.c"
        "gatt_exec.c"
        "ha_ota.c"
        "ha_relay.c"
    INCLUDE_DIRS "."
    REQUIRES nvs_flash esp_wifi esp_event esp_netif mqtt bt json
             app_update esp_https_ota esp_http_client mbedtls
             switchbot_decode ha_ble_scan
)
