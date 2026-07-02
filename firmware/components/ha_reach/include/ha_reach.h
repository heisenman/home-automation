#pragma once
#include <stdint.h>
#include <stdbool.h>

// Mesh reach census (ADR-0023) — decouples OBSERVATION from ACTUATION. A node maintains a per-MAC
// RSSI-EWMA table of every SwitchBot endpoint it hears, INDEPENDENT of its relay allowlist, and
// reports a compact snapshot on demand. This is metadata only (a small periodic summary), never the
// advert firehose — the relay filter and its Phase-B energy savings stay exactly as they are, so the
// coordinator regains full-neighborhood visibility (no fossils, organic rebalancing) at negligible cost.
//
// Cadence is SERVER-DRIVEN (ADR-0023 amendment): the coordinator pushes a signed trigger and the node
// reports, so the whole mesh censuses at a coordinated instant. A long autonomous fallback preserves the
// "no node is ever invisible" guarantee if the trigger path goes silent.
//
// Shared/pure like ha_ble_scan: it holds only the table + a low-rate fallback task and takes a publish
// callback — it depends on neither ha_mqtt nor ha_relay. The node wires the sink + the trigger.

typedef struct {
    const char *node_id;                       // for logging only
    void (*publish)(const char *reach_json);   // node sink: wraps the array + publishes home/edge/<node>/reach
    uint32_t fallback_ms;                      // self-report if no trigger within this (0 => default 30 min)
} ha_reach_cfg_t;

// Start the census: seeds the report clock + a low-rate fallback task. Call once, after ha_mqtt_start().
void ha_reach_start(const ha_reach_cfg_t *cfg);

// Feed one heard sighting (display-order MAC + RSSI in dBm). Called from the BLE observer's per-advert
// tap for EVERY heard SwitchBot endpoint, regardless of the relay allowlist. Runs on the scan hot path
// — keep it cheap. No-op-safe before ha_reach_start().
void ha_reach_note(const uint8_t mac[6], int rssi);

// Snapshot the table and publish it NOW as a JSON array [{mac,rssi_ewma,count,age_s},…]. Invoked by the
// node on a server trigger, and by the fallback task. Resets per-window sighting counts.
void ha_reach_report(void);
