# Platform-agnostic NimBLE observer. `bt` = NimBLE host (nimble/*, host/*). The
# controller (native vs esp-hosted VHCI) and the publish sink are caller-supplied
# hooks, so this pulls in neither esp_hosted nor ha_mqtt/ha_relay.
idf_component_register(SRCS "ha_ble_scan.c"
                       INCLUDE_DIRS "include"
                       REQUIRES bt switchbot_decode)
