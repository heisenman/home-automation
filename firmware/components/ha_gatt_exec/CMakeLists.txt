# ha_gatt_exec: shared generic GATT step-interpreter (ADR-0020). Promoted from edge/esp32c6/main/gatt_exec.c
# (byte-identical ×3). Platform deps (reply sink / diagnostic log) are cfg callbacks (ha_gatt_exec_init), so it
# depends on NEITHER ha_mqtt (edge) NOR the panel esp_mqtt — every NimBLE-host board links the same interpreter.
# Arbitrary GATT writes are gated at COMPILE TIME by CONFIG_HA_GATT_ALLOW_WRITE (default n) — see Kconfig.
idf_component_register(SRCS "ha_gatt_exec.c"
                       INCLUDE_DIRS "include"
                       REQUIRES bt ha_ble_scan json)
