# Operability status LED (WS2812 over RMT) — the fault-only LED policy (ha_led.h states). S3-ETH platform
# module (ADR-0020); kept out of ha_mqtt/ha_wifi which drive it via optional callbacks.
idf_component_register(SRCS "ha_led.c"
                       INCLUDE_DIRS "include"
                       REQUIRES esp_driver_rmt)
