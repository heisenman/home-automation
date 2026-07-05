// BREADCRUMB: firmware/components > ha_gatt_exec - generic GATT step-interpreter: connect + discover-all-chars + run server-composed steps (sub/write/writeseq/read/collect/delay) + stream replies. Any device interaction is expressed as data, no new firmware. Contract: ADR-0020. Parent: firmware/AGENTS.md.
// Promoted from edge/esp32c6/main/gatt_exec.c (byte-identical ×3). Platform seams (reply/log) are cfg callbacks (ha_gatt_exec_init), like ha_gatt.
// REUSE-WHEN: a NimBLE-host node (edge c3/c6/s3, or the panel) must run an arbitrary server-composed GATT interaction against a device beyond the fixed SwitchBot history pull that ha_gatt covers.
//
#pragma once
#include <stdbool.h>

// Generic GATT step-interpreter. The server composes a list of BLE steps (subscribe / write /
// write-sequence / read / collect / delay); this connects to `mac`, discovers all characteristics,
// runs the steps in order, and streams replies via cfg.publish_reply (edge: home/edge/<node>/<reqid>/reply).
//
// Platform-agnostic exactly like ha_gatt: the ONLY board differences — where a reply line goes and where
// diagnostics go — are callbacks. So this REQUIRES only `bt` + `ha_ble_scan` + `json`; it pulls in no ha_mqtt.
// Arbitrary GATT writes are an actuation primitive and are OFF unless the build sets
// CONFIG_HA_GATT_ALLOW_WRITE=y (compile-time least-privilege: a telemetry binary contains no write path).
typedef struct {
    // Relay one reply JSON line for reqid. Called on the exec task. Caller owns topic construction
    // (edge: home/edge/<node>/<reqid>/reply). NULL => replies are dropped.
    void (*publish_reply)(const char *reqid, const char *json, void *user);
    // Human-readable diagnostic line (connect status, parse errors). NULL => silent (still mirrored to ESP_LOG).
    void (*log)(const char *msg, void *user);
    void *user;
} ha_gatt_exec_cfg_t;

// Install the platform callbacks. *cfg is copied. Call once at boot before any run.
void ha_gatt_exec_init(const ha_gatt_exec_cfg_t *cfg);

// `steps_json` is the raw JSON array text of the "steps" field (copied; caller may free after).
// Returns false if the node is busy with another central-role op, the mac isn't in the scan cache,
// or the connect can't be started.
bool ha_gatt_exec_run(const char *reqid, const char *mac_str, const char *steps_json);

bool ha_gatt_exec_busy(void);
