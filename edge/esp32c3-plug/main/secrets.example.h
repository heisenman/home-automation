// Copy to secrets.h (git-ignored) and fill in — or let tools/enroll_node.py --from-manifest write it.
#pragma once

#define HA_WIFI_SSID    "autohome_airgap"
#define HA_WIFI_PSK     "your-wifi-password"

// MQTT broker = the failover VIP on the air-gap net.
#define HA_BROKER_URI   "mqtt://192.168.1.200:1883"

// This node's id — topic home/edge/<node>/... and meta.node.
#define HA_NODE_ID      "plug-bench"

// SNTP source on the air-gap net.
#define HA_NTP_SERVER   "192.168.1.210"

// OTA image host pin (ADR-0020). Edge OTAs originate from ha-2.
#define HA_OTA_HOST     "192.168.1.210"
