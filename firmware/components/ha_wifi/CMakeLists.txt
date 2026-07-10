# Wi-Fi STA bring-up with edge-relay robustness (reconnect-forever + a down-watchdog reboot). The status
# LED is an OPTIONAL caller-supplied callback (ha_wifi_set_status_cb), so this depends on no board module —
# a Wi-Fi-only node (c3/c6) and the Ethernet-capable s3 both link the same component (ADR-0020).
idf_component_register(SRCS "ha_wifi.c"
                       INCLUDE_DIRS "include"
                       REQUIRES esp_wifi esp_event esp_netif esp_timer)
