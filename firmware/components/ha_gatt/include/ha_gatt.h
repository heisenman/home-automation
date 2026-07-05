// BREADCRUMB: firmware/components > ha_gatt - shared GATT-central SwitchBot on-device-history client (connect + reverse-engineered handshake/paging + RAW-notification relay). Contract: ADR-0020. Parent: firmware/AGENTS.md.
// Promoted from edge/esp32c6/main/gatt_history.c (roadmap #5).
// REUSE-WHEN: any NimBLE-host node (edge c3/c6/s3 native controller, OR the D1001 panel over the esp_hosted HCI) needs to CONNECT to a SwitchBot meter and pull its on-device history log.
//
#pragma once
#include <stdbool.h>

// Shared GATT-central history client (ADR-0020). Brings the SwitchBot history pull — the transport half
// only: it connects, runs the handshake/paging, and relays the RAW notification bytes upward. The server
// (server/ingest/edge_history.py) does the authoritative decode/timestamp/insert. Single radio: the caller's
// passive ha_ble_scan is paused for the pull and resumed on disconnect (handled internally).
//
// Platform-agnostic exactly like ha_ble_scan: the ONLY platform differences — how a relay line reaches the
// broker, and where diagnostics go — are callbacks. So this REQUIRES only `bt` + `ha_ble_scan`; it pulls in
// neither ha_mqtt (edge-only) nor the panel's esp_mqtt client. The edge wires publish->ha_mqtt_publish_history
// + log->ha_mqtt_log; the panel wires publish->(esp_mqtt to home/edge/<node>/<macflat>/history) + log->its
// gatt topic. Wire format the server expects (unchanged):
//   {"t":"meta","mac":..,"newest_ts":..,"newest_ptr":..,"oldest_ts":..,"oldest_ptr":..,"start_addr":..,"pull_now":..}
//   {"t":"data","mac":..,"seq":k,"notifs":["<hex>",...]}      # batches of RAW record notifications
//   {"t":"done","mac":..,"count":N}
typedef struct {
    // Relay one JSON line (meta/data/done) for mac_str (display order "AA:BB:CC:DD:EE:FF"). Called on the
    // pull task. The caller owns topic construction (edge: home/edge/<node>/<macflat>/history).
    void (*publish)(const char *mac_str, const char *json, void *user);
    // Human-readable diagnostic line (connect status, discovery, per-attempt meta). NULL => silent.
    void (*log)(const char *msg, void *user);
    void *user;
} ha_gatt_cfg_t;

// Install the platform callbacks. *cfg is copied. Call once at boot before any pull/probe.
void ha_gatt_init(const ha_gatt_cfg_t *cfg);

// Connect to mac_str and pull its on-device history, relaying via cfg.publish. profile selects the wire
// dialect: "meter_pro" (indoor Meter/MeterPro) or "outdoor" (Outdoor Meter W&H, multi-bank; the default).
// window_records bounds the pull to the most-recent N records (0 = full range). On a shared-radio node
// (the panel: BLE+WiFi multiplexed over one C6 via esp_hosted) each read is ~5-8x slower than a native
// edge radio, so a full meter_pro range (thousands of reads) can't complete before a link-layer timeout;
// bounding to a recent window both completes fast AND keeps the newest records — so the server's
// newest-sample-≈-now freshness guard passes. Native edge nodes pass 0 for a full backfill.
// Returns false immediately if busy or the mac hasn't been heard advertising (addr not cached by the scan).
// The pull runs on its own task; completion is signalled by the {"t":"done"} relay line.
bool ha_gatt_history_pull(const char *mac_str, const char *profile, int window_records);

// Connect + discover services only (no handshake/pull) — the Spike-0 diagnostic that proved GATT-central
// works over the esp_hosted HCI. Cheap reachability/latency check. Progress -> cfg.log.
bool ha_gatt_probe(const char *mac_str);

// True while a pull or probe holds the radio.
bool ha_gatt_busy(void);
