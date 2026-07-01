#pragma once
// Copy to secrets.h (gitignored) and fill in. main/beachhead_main.c includes "secrets.h".
#define WIFI_SSID       "YOUR_SSID"                    // case-sensitive
#define WIFI_PASS       "YOUR_WIFI_PASSWORD"
#define MQTT_BROKER_URI "mqtt://192.168.0.210:1883"    // dictator real IP (VIP unreachable from wifi)
#define BFF_BASE_URL    "http://192.168.0.210:8123"    // FastAPI BFF (read endpoints open on LAN)
