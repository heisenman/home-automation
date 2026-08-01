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
    char gas_sensor[12];    // "sgp40"|"sgp30"|"bme680"|"none"|"auto"(default) — which gas chip is fitted.
                            // NVS-provisioned by the flasher, or left empty/"auto" to let the firmware
                            // probe the I2C bus and find out. Resolved via ha_gas_from_name().
    char bind_mac[18];      // "AA:BB:CC:DD:EE:FF" — the eFuse base MAC this NVS blob was minted FOR.
                            // Empty = unbound (legacy / hand-flashed). See ha_config_identity_ok().
    char cmd_secret[65];    // ADR-0036 Layer 0: the node's HMAC command secret, 64 hex chars + NUL.
                            // Precedence is NVS-FIRST, compile-time fallback (the reverse of every other
                            // key here): a node-born secret must never be shadowed by a stale build-time
                            // one. Empty on a generic image until ha_config_ensure_node_secret() runs.
} ha_config_t;

// Seed *cfg from *defaults (the board's compile-time secrets.h values, passed by app_main so this
// component stays secrets-free), then overlay any NVS-provisioned values (namespace "ha", keys
// wifi_ssid/wifi_psk/broker_uri/node_id/ntp_server/ota_host/cmd_secret). NVS wins where present
// (production provisioning). The loaded effective config is cached for ha_config_repoint_apply's backup.
void ha_config_load(ha_config_t *cfg, const ha_config_t *defaults);

// --- Identity binding (generic-image flashing) ------------------------------------------------------
// Does this NVS config actually belong to THIS chip? Returns true if cfg->bind_mac is empty (unbound —
// legacy nodes and hand-built images, unchanged behaviour) or matches the eFuse base MAC.
//
// This is the ADR-0020 anti-cross-provisioning gate, MOVED rather than dropped. Historically an image was
// branded for one node_id at build time, so flashing it to the wrong board was the hazard (the 2026-07-05
// coffice_c6-onto-cbed_c6 incident). With one generic image per target that hazard moves to the NVS blob:
// the wrong blob would give a board someone else's identity. Binding the blob to the eFuse MAC read off
// the physical chip at flash time makes that impossible — and it is STRONGER than the old gate, because
// the MAC comes from the silicon rather than from a manifest line a human typed.
//
// Call after ha_config_load and BEFORE anything uses the identity (MQTT, OTA). A mismatch means the board
// was re-flashed with a blob minted for a different chip; the caller should refuse to come up rather than
// impersonate another node.
bool ha_config_identity_ok(const ha_config_t *cfg, char *why, size_t why_sz);

// --- ADR-0036 Layer 0: node-born command secret -----------------------------------------------------
// Ensure this node HAS a command secret, generating one if it doesn't. Call ONCE, AFTER the network is
// up and BEFORE ha_mqtt_init.
//
// **The timing is load-bearing, not stylistic.** esp_random() is only a true hardware RNG once the radio
// is running; before that it degrades to a weak PRNG. Generating at cold boot would mint predictable
// secrets across a whole fleet of identical units. Call this only after ha_wifi_connect()/ha_eth has
// succeeded.
//
// Precedence, in order:
//   1. an NVS secret (namespace "ha", key "cmd_secret") — a node-born or provisioned secret, wins;
//   2. else the compile-time secret in cfg->cmd_secret (a LEGACY enrolled node, ADR-0020) — kept as-is
//      and NEVER overwritten, so flashing this firmware to the existing fleet changes nothing;
//   3. else generate: secret = HMAC_SHA256(key = 32 random bytes, msg = base MAC), hex-encoded.
//      The 256-bit random is what makes it unguessable; the MAC is a domain separator giving
//      uniqueness-by-construction. The secret is NOT derived from the MAC alone — a MAC is public, so
//      that would be predictable.
//
// Writes through to cfg->cmd_secret either way, so the caller can hand it straight to ha_mqtt_init.
// Returns true if *cfg ends up holding a usable secret. Idempotent across reboots (case 1 after the
// first run). At 256 bits the birthday bound is ~2^128, so chance collision is not a concern.
bool ha_config_ensure_node_secret(ha_config_t *cfg);

// True once this node has been CLAIMED by a dictator (ADR-0036 Layer 3 TOFU), or if it carries a
// legacy compile-time secret (already enrolled by construction). Drives `hello.enrolled`, which is what
// the PWA uses to tell adoptable hardware from adopted. NB this is deliberately NOT "do I hold a
// secret" — with Layer 0 every node self-provisions a secret on first boot, so holding one no longer
// implies anyone knows it.
bool ha_config_is_claimed(void);

// Record that this node has been claimed (persists to NVS). Called by the enroll handler after it hands
// the secret to the dictator exactly once. Returns false if the NVS write failed — the caller MUST then
// treat the claim as not having happened, or a reboot would re-open the one-shot window.
bool ha_config_mark_claimed(void);

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
