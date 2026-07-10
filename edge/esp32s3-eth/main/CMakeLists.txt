idf_component_register(
    SRCS
        "app_main.c"
        "ha_eth.c"
        "ha_led.c"
    INCLUDE_DIRS "."
    REQUIRES nvs_flash esp_eth esp_wifi driver esp_driver_rmt esp_event esp_netif mqtt bt json
             app_update esp_https_ota esp_http_client mbedtls
             switchbot_decode ha_ble_scan ha_config ha_mqtt ha_gatt ha_gatt_exec ha_ota ha_relay ha_sntp ha_reach ha_wifi ha_gas
             sgp40 sensirion_gas_index   # pulled in via ha_gas; listed so the module matrix records real binary contents
)
