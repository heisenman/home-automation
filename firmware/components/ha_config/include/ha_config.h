// BREADCRUMB: firmware/components > ha_config - node configuration loader: seed from the board's compile-time defaults (secrets.h), then overlay NVS-provisioned values (namespace "ha"). Contract: ADR-0020. Parent: firmware/AGENTS.md.
// Promoted from edge/esp32c6/main/ha_config.c (byte-identical ×3). The per-node secrets.h stays board-local (the seam): app_main passes the compiled defaults in, so this component never includes secrets.
// REUSE-WHEN: any node needs its identity/broker/wifi/ntp resolved as compile-time-default-then-NVS-overlay (don't re-implement the NVS overlay + provisioning precedence).
#pragma once
#include <stddef.h>

typedef struct {
    char wifi_ssid[33];
    char wifi_psk[64];
    char broker_uri[96];
    char node_id[32];
    char ntp_server[64];
} ha_config_t;

// Seed *cfg from *defaults (the board's compile-time secrets.h values, passed by app_main so this
// component stays secrets-free), then overlay any NVS-provisioned values (namespace "ha", keys
// wifi_ssid/wifi_psk/broker_uri/node_id/ntp_server). NVS wins where present (production provisioning).
void ha_config_load(ha_config_t *cfg, const ha_config_t *defaults);
