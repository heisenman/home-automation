# ha_wifi — Wi-Fi STA bring-up (shared, ADR-0020)

Blocking `ha_wifi_connect(ssid, psk, timeout_ms)` with edge-relay robustness: reconnect-forever + a
down-watchdog reboot (no IP for ~2 min → `esp_restart`, so app_main re-runs its network auto-sense).
Self-inits `esp_netif` + the default event loop **idempotently**, so it works whether or not app_main
already did (a Wi-Fi-only node vs the Ethernet-capable s3 that shares the stack).

Status LED is an **optional** caller callback — `ha_wifi_set_status_cb(cb)` — so this component depends on
no board peripheral (s3 wires it to `ha_led`; c3/c6 leave it NULL).
