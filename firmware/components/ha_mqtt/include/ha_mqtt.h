// BREADCRUMB: firmware/components > ha_mqtt - edge MQTT client: signed-command verify (HMAC) + monotonic anti-replay + dispatch (history/gatt/ota), relay + reach directives, and the node's publish paths (adv/history/reply/reach/log). Contract: ADR-0010, 0023. Parent: firmware/AGENTS.md.
// Promoted from edge/esp32c6/main/ha_mqtt.c (was drifted ×3; the drift was board hooks, not core). Board bits are the ha_mqtt_cfg_t seam: per-node secrets (passed by app_main from secrets.h), optional LED status hooks, and the reach feature flag. The gatt/ota/gatt_exec platform seams are wired INTERNALLY (they call this component's own publish/log), so they're no longer per-board.
// REUSE-WHEN: any edge node needs the signed-command/relay/OTA control plane + the canonical home/edge/<node>/* publish topics. Don't re-implement the HMAC verify + anti-replay + dispatch.
#pragma once
#include <stdbool.h>
#include "switchbot_decode.h"

// Board seam: per-node secrets (app_main reads the board-local secrets.h and passes them — the component
// stays secrets-free), the reach feature flag (nodes without ha_reach wired set false), and optional
// status hooks (e.g. the S3's operability LED). All hook pointers may be NULL.
typedef struct {
    const char *cmd_secret;    // HA_CMD_SECRET — "" ⇒ every signed command rejected (un-enrolled node)
    const char *ota_host;      // HA_OTA_HOST — pinned OTA image host ("" disables the host pin)
    const char *mqtt_user;     // HA_MQTT_USER — "" ⇒ anonymous (latent until broker-auth cutover)
    const char *mqtt_pass;     // HA_MQTT_PASS
    const char *fw_version;    // HA_FW_VERSION — human tag in the "online <slot> <ver>" status message
    bool enable_reach;         // ADR-0023 mesh reach census — false on nodes that don't wire ha_reach
    void (*on_connected)(void *user);      // MQTT connected (S3: LED = OK). NULL ⇒ no-op.
    void (*on_disconnected)(void *user);   // MQTT dropped   (S3: LED = MQTT_DOWN). NULL ⇒ no-op.
    void (*ota_on_fail)(void *user);       // OTA reject/fail (S3: LED = OTA_FAIL). NULL ⇒ no-op.
    void *user;
} ha_mqtt_cfg_t;

// Install the board seam (secrets, flags, hooks). *cfg is copied. Call once at boot BEFORE ha_mqtt_start
// (and before ha_mqtt_has_cmd_secret, which some app_mains check early to gate on enrollment).
void ha_mqtt_init(const ha_mqtt_cfg_t *cfg);

// True if a command secret is provisioned (cfg.cmd_secret non-empty) — the node can accept signed cmds/OTA.
bool ha_mqtt_has_cmd_secret(void);

// Start the MQTT client (retained LWT on home/edge/<node>/status = "offline"). Wires the shared
// ha_gatt / ha_gatt_exec / ha_ota platform seams internally. Call after ha_mqtt_init + network up.
void ha_mqtt_start(const char *broker_uri, const char *node_id);
bool ha_mqtt_is_connected(void);

// Publish a decoded reading keyed by MAC to home/edge/<node>/<mac>/adv. mac_str is "AA:BB:..", rssi dBm.
void ha_mqtt_publish_reading(const char *mac_str, const sb_reading_t *r, int rssi);

// Generic variant: same envelope/topic, but with a caller-built device_type + metrics JSON string (e.g.
// a non-SwitchBot decoder like Aranet). metrics_json is the inner object, e.g. {"radon_bqm3":42,...}.
void ha_mqtt_publish_device(const char *mac_str, const char *device_type, const char *metrics_json, int rssi);

// Publish a raw history-relay message to home/edge/<node>/<mac>/history (qos 1).
void ha_mqtt_publish_history(const char *mac_str, const char *payload);

// Publish a generic GATT-forwarder reply to home/edge/<node>/<reqid>/reply (qos 1).
void ha_mqtt_publish_reply(const char *reqid, const char *payload);

// Publish a NODE-LOCAL (non-BLE) sensor reading to home/edge/<node>/<key>/adv (qos 1). `key` = topic
// segment, `reg_key` = registry lookup key in the payload "mac" field, transport tagged "i2c-local".
void ha_mqtt_publish_node_sensor(const char *key, const char *reg_key,
                                 const char *device_type, const char *metrics_json);

// Publish a mesh reach census (ADR-0023) to home/edge/<node>/reach. `reach_json` is a pre-formed JSON
// ARRAY from ha_reach; this wraps it with schema/node/ts.
void ha_mqtt_publish_reach(const char *reach_json);

// Remote log line to home/edge/<node>/log (qos 0) — debug a headless node over MQTT.
void ha_mqtt_log(const char *fmt, ...);
