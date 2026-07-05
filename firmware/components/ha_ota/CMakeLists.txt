# ha_ota: shared OTA client (ADR-0020) — host pin + node-id identity gate + signed-hash + self-test/
# rollback over esp_https_ota. Platform deps (log / health / radio / LED) are cfg callbacks (ha_ota_init),
# so it depends on NEITHER ha_mqtt (edge) NOR ble_scan NOR ha_led — every board links the same OTA path.
idf_component_register(SRCS "ha_ota.c"
                       INCLUDE_DIRS "include"
                       REQUIRES app_update esp_https_ota esp_http_client esp_partition mbedtls esp_system)
