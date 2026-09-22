// Copy to secrets.h (git-ignored) and fill in. Bench provisioning path.
// Production path is NVS provisioning (tools/enroll_node.py --from-manifest); secrets.h keeps creds out
// of git for dev.
#pragma once

#define HA_WIFI_SSID    "autohome_airgap"
#define HA_WIFI_PSK     "your-wifi-password"

// MQTT broker = the failover VIP on the air-gap net.
#define HA_BROKER_URI   "mqtt://192.168.1.200:1883"

// This node's id — appears in topic home/edge/<node>/... and meta.node.
#define HA_NODE_ID      "c6-dehum-bench"

// SNTP source. Air-gapped: .210's chrony on its air-gap leg.
#define HA_NTP_SERVER   "192.168.1.245"

// OTA image host pin (ADR-0020). Edge C6/S3 OTAs originate from ha-2.
#define HA_OTA_HOST     "192.168.1.210"
