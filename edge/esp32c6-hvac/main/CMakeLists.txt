idf_component_register(
    SRCS
        "app_main.c"
        "jsonbuf.c"
    INCLUDE_DIRS "."
    REQUIRES nvs_flash esp_wifi esp_event esp_netif mqtt json esp_timer driver
             app_update esp_https_ota esp_http_client mbedtls
             ha_config ha_mqtt ha_ota ha_sntp ha_wifi ha_rs485 ha_broan
)
