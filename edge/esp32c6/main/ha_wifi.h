#pragma once
#include "esp_err.h"
// Bring up Wi-Fi STA and block until an IP is obtained (or timeout_ms elapses).
esp_err_t ha_wifi_connect(const char *ssid, const char *psk, int timeout_ms);
