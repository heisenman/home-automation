idf_component_register(
    SRCS
        "app_main.c"
        "ha_config.c"
        "ha_wifi.c"
        "ha_sntp.c"
        "ha_mqtt.c"
        "ble_scan.c"
        "gatt_history.c"
        "switchbot_decode.c"
    INCLUDE_DIRS "."
    REQUIRES nvs_flash esp_wifi esp_event esp_netif mqtt bt json
)
