// BREADCRUMB: firmware/components > ha_ota - shared OTA client: signed-hash authenticity + node-id identity gate + self-test/auto-rollback, over esp_https_ota. Contract: ADR-0020. Parent: firmware/AGENTS.md.
// REUSE-WHEN: any node that accepts firmware over the air (edge c3/c6/s3, the D1001 panel) needs the pinned-host + signed-hash + per-node identity gate before an image is allowed to boot.
#pragma once
#include <stdbool.h>

// Over-the-air update via HTTP pull into the inactive OTA slot, with three gates and auto-rollback.
//
// Gates (all BEFORE the new image is allowed to boot):
//   1. host pin       — the URL host must equal cfg.ota_host ("" disables).
//   2. identity       — the image's app_desc.version must be branded "<cfg.node_id>@…" (ADR-0020):
//                       refuse an image built for a DIFFERENT node (the 2026-07-05 cross-provisioning bug).
//   3. signed hash    — the written bytes must match the directive's signed SHA-256 (anti-malice).
// Brick-safety: a freshly-OTA'd image boots PENDING_VERIFY; ha_ota_confirm_if_pending() self-tests
// (cfg.is_healthy) and only then marks it valid — else the bootloader rolls back. Cable-flashed images
// are never PENDING_VERIFY, so confirm is a no-op on bench builds.
//
// Platform seams (cfg) keep this component free of ha_mqtt / ble_scan / ha_led so every board — and the
// panel — links the SAME OTA path instead of a fork (ADR-0020).
typedef struct {
    const char *node_id;    // this node's id — identity gate rejects images not branded "<node_id>@…"
    const char *ota_host;   // pinned image host, e.g. "192.168.0.210" ("" or NULL disables the pin)
    void (*log)(const char *msg, void *user);     // diagnostic sink (edge: ha_mqtt_log). Mirrored to ESP_LOG.
    bool (*is_healthy)(void *user);               // self-test predicate (edge: broker connected). NULL → skip.
    void (*radio_pause)(void *user);              // optional: single-radio nodes pause BLE during the pull
    void (*radio_resume)(void *user);             // optional
    void (*on_fail)(void *user);                  // optional: called on any reject/failure (e.g. LED feedback)
    void *user;
} ha_ota_cfg_t;

// Install the platform callbacks + identity. *cfg is copied. Call once at boot before any OTA.
void ha_ota_init(const ha_ota_cfg_t *cfg);

// Call once at boot AFTER the network is started. Confirms or rolls back a trial image.
void ha_ota_confirm_if_pending(void);

// Start an OTA download from `url` into the inactive slot. On success the node reboots into the new slot
// pending verification; on any failed gate it stays on the current image. False if busy or task can't start.
bool ha_ota_start(const char *url, const char *expected_sha256);

bool ha_ota_busy(void);
