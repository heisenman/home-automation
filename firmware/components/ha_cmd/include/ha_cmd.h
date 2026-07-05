// BREADCRUMB: firmware/components > ha_cmd - shared signed-directive verifier (HMAC-SHA256 {p,s} envelope + freshness window + monotonic (ts,seq) anti-replay persisted in NVS). Contract: ADR-0010. Parent: firmware/AGENTS.md.
// REUSE-WHEN: any device that ACTS on an authority directive (edge nodes, the D1001 panel) must verify the signed {p,s} envelope before acting, instead of reinventing the scheme.
//
// ha_cmd — signed-directive verification (ADR-0010), shared firmware component.
//
// Ports the PROVEN edge-node verifier (edge/esp32c6/main/ha_mqtt.c, live in v11-nonce) into a reusable
// component so a second device (the D1001 panel) enforces the exact same scheme instead of reinventing
// it. Every authority directive is an HMAC-SHA256-signed {p,s} envelope; the device acts only if the
// signature verifies (constant-time), the timestamp is fresh, and the (ts,seq) is strictly newer than the
// last it acted on (monotonic anti-replay persisted in NVS). Anything failing is refused. HMAC over PKI:
// offline-first, per-device secret, cheap on an MCU (mbedtls already linked).
//
// Wire format (from tools/edge_sign.py): the message is {"p":"<compact-json inner>","s":"<hmac hex>"},
// signature = HMAC-SHA256(secret, the LITERAL p string). Inner carries {"op":...,"ts":<unix>,"seq":<n>,
// ...op args}. Freshness window: 300 s for actuation, 86400 s for "ota" (a clock-drifted device can still
// be OTA-recovered — the image is hash-verified + anti-downgrade, so a replay just re-flashes the same).
#pragma once
#include <stdbool.h>
#include "cJSON.h"

typedef enum {
    HA_CMD_OK = 0,        // signature + freshness + anti-replay all passed; *inner_out is set
    HA_CMD_BAD_JSON,      // payload isn't parseable JSON
    HA_CMD_UNSIGNED,      // no {p,s} envelope — signature is REQUIRED, refuse
    HA_CMD_NO_SECRET,     // this device has no HA_CMD_SECRET provisioned — cannot verify, refuse
    HA_CMD_BAD_SIG,       // HMAC mismatch (forged / wrong secret / tampered p)
    HA_CMD_STALE,         // |now - ts| exceeds the freshness window
    HA_CMD_REPLAY,        // (ts,seq) not strictly newer than the last acted-on command
} ha_cmd_result_t;

// Verify a signed-command envelope. `payload`/`len` = the raw MQTT message; `secret` = this device's
// per-device HA_CMD_SECRET (empty/NULL => HA_CMD_NO_SECRET). On HA_CMD_OK, *inner_out is a NEW cJSON of
// the verified inner object (the caller dispatches on inner["op"] and frees it with cJSON_Delete). On any
// failure *inner_out is set to NULL. The freshness window is chosen from the inner "op" (wide for "ota").
// The monotonic (ts,seq) high-water mark is loaded/stored in NVS namespace "ha_cmd" (shared across ops).
ha_cmd_result_t ha_cmd_verify(const char *payload, int len, const char *secret, cJSON **inner_out);

// Short human string for a result code (logging / ack text).
const char *ha_cmd_result_str(ha_cmd_result_t r);
