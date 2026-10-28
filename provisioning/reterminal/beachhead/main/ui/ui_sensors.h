// D1001 "Sensors" view (Catherine, 2026-10-27) — every sensor's LATEST readings in the E1001 room row's
// columns: Temp | Hum | Dew | Air | notable tail. Default is a grid with ONE ROW PER ROOM; where a room has
// several sensors with a value, the values stack one above the other in that column (2026-10-28). The Edit
// button switches to the per-sensor list (one row per sensor, grouped by room) where sensors are hidden.
//
// Source: GET /api/v1/panel/sensors (server/api/viewmodel.py build_panel_sensors — server-authored rows,
// temps in °C, converted here for display). Each row has a hide button; hidden sensors are THIS PANEL's
// choice (NVS namespace "senslist"), not a house-wide setting — the Devices screen's Hidden status is the
// house-wide one. Hidden sensors drop out of the room grid; Edit mode shows them dimmed with an un-hide button.
#pragma once
#include "lvgl.h"
#include "cJSON.h"

// Load the hidden set from NVS. Call once at start (before the first render).
void ui_sensors_init(void);

// (Re)build the list into `parent` from a parsed /api/v1/panel/sensors document (`doc`, .sensors[]); NULL =
// offline note. Keeps the scroll position across the periodic refresh. Caller holds the LVGL lock.
// `changed` is called (under the lock, from a click) when a hide/un-hide/toggle needs a re-render.
void ui_sensors_render(cJSON *doc, lv_obj_t *parent, void (*changed)(void));
