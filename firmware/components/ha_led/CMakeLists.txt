# Operability status LED (WS2812 over RMT) — the fault-only LED policy (ha_led.h states). S3-ETH platform
# module (ADR-0020); kept out of ha_mqtt/ha_wifi which drive it via optional callbacks.
# esp_driver_gpio is for ha_led_blank(), which parks a blanked WS2812's data line driven LOW rather than
# releasing it — a floating data pin is how a stale colour gets latched back in.
idf_component_register(SRCS "ha_led.c"
                       INCLUDE_DIRS "include"
                       REQUIRES esp_driver_rmt
                       PRIV_REQUIRES esp_driver_gpio)
