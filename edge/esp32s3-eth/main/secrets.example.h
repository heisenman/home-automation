// Copy to secrets.h (git-ignored) and fill in. Bench provisioning path.
// Production path is NVS provisioning (see README); secrets.h keeps creds out of git for dev.
#pragma once

#define HA_WIFI_SSID    "your-ssid"
#define HA_WIFI_PSK     "your-wifi-password"

// MQTT broker = the dictator VIP — the FLOATING dictator address (192.168.0.200), never a fixed box, so
// the node follows whichever server (210/.245) is dictator across a failover (ADR-0015 Phase 0). The
// broker listens on 0.0.0.0, so the VIP holder answers automatically.
#define HA_BROKER_URI   "mqtt://192.168.0.200:1883"

// This node's id — appears in topic home/edge/<node>/<mac>/adv and meta.node.
#define HA_NODE_ID      "c6-bench"

// SNTP source. Bench (online): a pool server. Air-gapped: the dictator running chrony.
#define HA_NTP_SERVER   "pool.ntp.org"
