// BREADCRUMB: firmware/components > ha_config - node configuration loader: seed from the board's compile-time defaults (secrets.h), then overlay NVS-provisioned values (namespace "ha"). Contract: ADR-0020. Parent: firmware/AGENTS.md.
// Promoted from edge/esp32c6/main/ha_config.c (byte-identical ×3). The per-node secrets.h stays board-local (the seam): app_main passes the compiled defaults in, so this component never includes secrets.
// REUSE-WHEN: any node needs its identity/broker/wifi/ntp resolved as compile-time-default-then-NVS-overlay (don't re-implement the NVS overlay + provisioning precedence).
#pragma once
#include <stddef.h>
#include <stdbool.h>

typedef struct {
    char wifi_ssid[33];
    char wifi_psk[64];
    char broker_uri[96];
    char node_id[32];
    char ntp_server[64];
    char ota_host[64];      // OTA host pin (ADR-0010). Overlaid from NVS "ha" key "ota_host" so a repoint
                            // can move it; app_main seeds the compile-time default (HA_OTA_HOST).
} ha_config_t;

// Seed *cfg from *defaults (the board's compile-time secrets.h values, passed by app_main so this
// component stays secrets-free), then overlay any NVS-provisioned values (namespace "ha", keys
// wifi_ssid/wifi_psk/broker_uri/node_id/ntp_server/ota_host). NVS wins where present (production
// provisioning). The loaded effective config is cached for ha_config_repoint_apply's backup.
void ha_config_load(ha_config_t *cfg, const ha_config_t *defaults);

// --- Repoint (air-gap migration; the signed "repoint" cmd, ADR-0028/DJ-19) -------------------------
// Move this node to a new Wi-Fi + broker by rewriting the "ha" overlay, then rebooting. node_id is
// PRESERVED (identity is fixed; the OTA gate keys off it). Backs up the current effective config and arms
// a boot-count revert (see below) so a bad repoint self-heals over the air. Only `broker` is required;
// ssid/psk/ntp/ota_host are optional (NULL/"" = leave that key unchanged) — a WIRED node (S3-ETH)
// repoints broker-only and keeps its Wi-Fi fallback creds. On success this REBOOTS (does not return);
// returns false only if the NVS write failed, in which case NOTHING changed and the node stays put.
bool ha_config_repoint_apply(const char *ssid, const char *psk, const char *broker,
                             const char *ntp, const char *ota_host);

// Call EARLY in boot — right after ha_config_load and BEFORE Wi-Fi connect. If a repoint is pending it
// counts this boot; once too many trial boots fail it reverts to the backed-up config and reboots. The
// early placement is deliberate: a bad SSID/PSK fails ha_wifi_connect and reboots before any late hook
// runs, so only an EARLY counter can catch that failure mode. No-op if no repoint is pending.
void ha_config_repoint_boot_check(void);

// Call LATE in boot (after MQTT start), analogous to ha_ota_confirm_if_pending. If a repoint is pending,
// poll is_healthy(user) (broker reachable) for up to timeout_ms: connected -> clear the pending state
// (repoint committed); timed out -> reboot to retry (-> eventual revert via the boot counter). No-op if
// none pending.
void ha_config_repoint_confirm(bool (*is_healthy)(void *user), void *user, int timeout_ms);
