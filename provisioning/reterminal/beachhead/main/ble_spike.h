#pragma once
#include <stdint.h>
#include <stdbool.h>

// Spike 0 (ADR-0019 Phase 6): prove NimBLE-over-esp-hosted-VHCI actually receives
// BLE adverts through the factory C6 slave. One-shot, non-fatal, MQTT-triggered.

// Init the hosted BT controller (over VHCI) + NimBLE host, then start a passive
// observer scan. Logs errors and returns cleanly on any failure (never aborts).
void ble_spike_start(void);

// Rolling counters for telemetry. total = adverts seen; uniq = distinct MACs
// (counts beyond the storage cap); last_rssi = newest advert RSSI.
void ble_spike_stats(uint32_t *total_adv, uint32_t *uniq, int8_t *last_rssi);

// True once the passive scan is confirmed running (on_sync fired + ble_gap_disc ok).
bool ble_spike_running(void);
