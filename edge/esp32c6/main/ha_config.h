// Node configuration. Bench: compile-time from secrets.h. Production: NVS-provisioned
// (namespace "ha", keys wifi_ssid/wifi_psk/broker_uri/node_id/ntp_server) overrides defaults.
#pragma once

typedef struct {
    char wifi_ssid[33];
    char wifi_psk[64];
    char broker_uri[96];
    char node_id[32];
    char ntp_server[64];
} ha_config_t;

// Loads compile-time defaults (secrets.h), then overlays any NVS-provisioned values.
void ha_config_load(ha_config_t *cfg);
