// Sensor tile grid — extracted from ui_tiles.c (ADR-0020 module-first). Owns the per-card
// registry (headline widget + spec) so live MQTT state can patch a card in place, and wires
// tap-to-expand. Deps: ui_format (catalog/helpers), ui_expand (opens a panel from a seed).
#pragma once
#include "lvgl.h"
#include "cJSON.h"

// Reset the sensor-card registry: free each card's detail heap + zero the count. Caller holds
// the LVGL lock and separately lv_obj_clean()s the shared grid container.
void ui_grid_reset(void);

// Build one sensor card into `parent` from a /api/v1/sensors entry, registering it for live
// MQTT headline patching + tap-to-expand. Caller holds the LVGL lock (render()).
void ui_grid_add_card(cJSON *e, lv_obj_t *parent);

// Patch the matching card's headline from an MQTT state payload. Takes the LVGL lock itself;
// runs on state_task, never on the MQTT callback stack.
void ui_grid_apply_state(const char *json);
