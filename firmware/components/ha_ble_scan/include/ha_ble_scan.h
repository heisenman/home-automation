#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "nimble/ble.h"
#include "host/ble_gap.h"
#include "switchbot_decode.h"

// Shared passive BLE observer (ADR-0020). Brings up the NimBLE host, runs a continuous
// passive scan, parses adverts, decodes SwitchBot meters (switchbot_decode), debounces
// per-MAC, and hands each fresh reading to a platform sink.
//
// Transport/controller-agnostic by design: the ONLY genuine platform difference — native
// BLE controller (edge nodes) vs esp-hosted controller over VHCI (D1001 panel) — is a
// hook, and the publish target (edge ha_mqtt+relay vs panel canonical adv publish) is a
// callback. So this component REQUIRES only `bt` + `switchbot_decode`; it pulls in neither
// esp_hosted (panel-only) nor ha_mqtt/ha_relay (edge-only).
//
// Discipline: runs on its own NimBLE host task, never on a caller's stack; every failure
// path logs and returns (never aborts) so a bad BLE bring-up can't drop the net/OTA lifeline.

typedef struct {
    // Bring up the BLE controller BEFORE nimble_port_init(). Return ESP_OK on success.
    //   NULL  => native controller (nimble_port_init handles it) — edge c3/c6/s3.
    //   panel => esp_hosted_bt_controller_init() + _enable() over VHCI.
    esp_err_t (*controller_init)(void);

    // Called once per FRESH (post-dedup) decoded reading, on the NimBLE host task.
    // mac_str is display order "AA:BB:CC:DD:EE:FF".
    //   edge  => if (ha_relay_allowed) ha_mqtt_publish_reading(...)
    //   panel => publish canonical home/edge/<node>/<mac>/adv
    void (*on_reading)(const char *mac_str, const sb_reading_t *r, int rssi, void *user);
    void *user;
} ha_ble_scan_cfg_t;

// Start the observer. *cfg is copied. Non-fatal: logs + returns on any failure.
void ha_ble_scan_start(const ha_ble_scan_cfg_t *cfg);

// Pause/resume the passive scan — frees the single radio for a GATT pull (Stage 2).
void ha_ble_scan_pause(void);
void ha_ble_scan_resume(void);

// True once the passive scan is confirmed running (on_sync fired + ble_gap_disc ok).
bool ha_ble_scan_running(void);

// Lightweight observability counters. total = all adverts seen; decoded = fresh readings
// handed to the sink; last_rssi = newest advert RSSI. Any out param may be NULL.
void ha_ble_scan_stats(uint32_t *total_adv, uint32_t *decoded, int8_t *last_rssi);

// own address type inferred at sync (for ble_gap_connect / GATT — Stage 2).
uint8_t ha_ble_own_addr_type(void);

// Full BLE address (type + LE value) the scanner last saw for mac_str. Returns false if
// not seen. GATT connect needs the address type. (Stage 2.)
bool ha_ble_lookup_addr(const char *mac_str, ble_addr_t *out);
