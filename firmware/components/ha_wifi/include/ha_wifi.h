#pragma once
#include <stdbool.h>
#include "esp_err.h"

// Optional link-status callback (e.g. to drive a board fault LED). up=false: link lost / reconnecting;
// up=true: got an IP. Default: none. Register BEFORE ha_wifi_connect. Boards without an LED skip it.
typedef void (*ha_wifi_status_cb_t)(bool up);
void ha_wifi_set_status_cb(ha_wifi_status_cb_t cb);

// Bring up Wi-Fi STA and block until an IP is obtained (or timeout_ms elapses). Edge-relay robustness:
// reconnects FOREVER on every disconnect (no retry cap), and if there is no IP for ~2 min it reboots to
// recover (app_main then re-runs its network auto-sense — so an Ethernet cable plugged in during a Wi-Fi
// outage is picked up too). Self-inits esp_netif + the default event loop IDEMPOTENTLY, so it is safe
// whether or not app_main already did that (e.g. an Ethernet-capable node that shares the stack).
esp_err_t ha_wifi_connect(const char *ssid, const char *psk, int timeout_ms);
