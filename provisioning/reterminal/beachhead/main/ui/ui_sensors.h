// D1001 "Sensors" view (Catherine, 2026-10-27) — every sensor's LATEST readings in the E1001 room row's
// columns: Temp | Hum | Dew | Air | notable tail. Default is a grid with ONE ROW PER ROOM; where a room has
// several sensors with a value, the values stack one above the other in that column (2026-10-28). The Edit
// button switches to the per-sensor list (one row per sensor, grouped by room) where sensors are hidden.
//
// Source: GET /api/v1/panel/sensors (server/api/viewmodel.py build_panel_sensors — server-authored rows,
// temps in °C, converted here for display). Each row has a hide button; hidden sensors are THIS PANEL's
// choice (NVS namespace "senslist"), not a house-wide setting — the Devices screen's Hidden status is the
// house-wide one. Hidden sensors drop out of the room grid; Edit mode shows them dimmed with an un-hide button.
// Edit mode also has up/down arrows on each room heading: the room order is this panel's too (NVS "rooms").
// In the grid, tapping Room/Temp/Hum/Dew/Air sorts the rows (high-to-low first, tap again to flip); "Your
// order" returns to the arrow order. The sort is not saved.
// Double-tapping a value in the grid (not the Edit list) opens that sensor's 24 h graph (2026-10-30): Temp,
// Hum and Dew graph themselves, Air graphs air_quality, and Other opens a ui_pick menu of the sensor's extra
// readings (straight to the graph when it has only one).
#pragma once
#include "lvgl.h"
#include "cJSON.h"

// Load the hidden set from NVS. Call once at start (before the first render).
void ui_sensors_init(void);

// (Re)build the list into `parent` from a parsed /api/v1/panel/sensors document (`doc`, .sensors[]); NULL =
// offline note. Keeps the scroll position across the periodic refresh. Caller holds the LVGL lock.
// `changed` is called (under the lock, from a click) when a hide/un-hide/toggle needs a re-render.
// `open_graph` is called (under the lock, from a double tap) with a sensor's device id + metric key.
// `doc` is copied, so the caller may free it once this returns.
void ui_sensors_render(cJSON *doc, lv_obj_t *parent, void (*changed)(void),
                       void (*open_graph)(const char *device_id, const char *metric));
