idf_component_register(
    SRCS
        "app_main.c"
        "ha_config.c"
        "ha_eth.c"
        "ha_wifi.c"
        "ha_sntp.c"
        "ha_mqtt.c"
        "ble_scan.c"
        "gatt_history.c"
        "gatt_exec.c"
        "ha_ota.c"
        "ha_led.c"
        "ha_relay.c"
        "switchbot_decode.c"
    INCLUDE_DIRS "."
    REQUIRES nvs_flash esp_eth esp_wifi driver esp_driver_rmt esp_event esp_netif mqtt bt json
             app_update esp_https_ota esp_http_client mbedtls
)
