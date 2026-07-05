# ha_gatt: shared GATT-central SwitchBot history client (ADR-0020, roadmap #5). Transport-agnostic —
# depends only on the NimBLE host (`bt`) and the shared passive observer (ha_ble_scan) for radio
# pause/resume + addr lookup + own-addr-type. The relay sink + diagnostic log are cfg callbacks
# (ha_gatt_init), so it pulls in NEITHER ha_mqtt (edge) NOR the panel's esp_mqtt client.
idf_component_register(SRCS "ha_gatt.c"
                       INCLUDE_DIRS "include"
                       REQUIRES bt ha_ble_scan)
