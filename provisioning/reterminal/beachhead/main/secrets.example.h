#pragma once
// Copy to secrets.h (gitignored) and fill in. main/beachhead_main.c includes "secrets.h".
#define WIFI_SSID       "YOUR_SSID"                    // case-sensitive
#define WIFI_PASS       "YOUR_WIFI_PASSWORD"
#define MQTT_BROKER_URI "mqtt://192.168.0.210:1883"    // dictator real IP (VIP unreachable from wifi)
#define BFF_BASE_URL    "http://192.168.0.210:8123"    // FastAPI BFF (read endpoints open on LAN)
// Scoped OPERATOR JWT for issuing device commands (mint on the dictator with
// tools/mint_panel_token.py). Leave "" to keep the panel read-only (no control buttons).
#define PANEL_TOKEN     ""                              // operator bearer; refresh via /auth/refresh
// Wall clock (ability G). Optional — beachhead_main.c has committed defaults; override here if needed.
// #define NTP_SERVER   "192.168.0.210"                 // LAN time anchor (dictator serves NTP via chrony)
// #define PANEL_TZ     "PST8PDT,M3.2.0,M11.1.0"        // POSIX TZ for the displayed local time
