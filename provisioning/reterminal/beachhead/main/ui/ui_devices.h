// Devices-management screen (ADR-0026 location / panel-ui-spatial-nav) — the coarse, canonical step
// of the location model: list every device under its CURRENT room, tap one to reassign its room.
//
// Coarse only (room), by design: the fine "exact spot within the room" (x,y) is web-only — the
// constrained panel does the cheap, canonical room assignment; the PWA does the rich geometry. Reads
// the same /api/v1/rooms document the house-map uses (rooms[].devices[] already grouped) and, on a
// pick, calls back with the chosen area — the orchestrator fires POST /devices/{id}/relocate through
// the admin worker (HTTP off the click stack). Rebuilt from scratch each render, like ui_map.
#pragma once
#include "lvgl.h"
#include "cJSON.h"

// Pick callback: the user chose `new_area` (canonical id + display name) for `device_id`. Pointers are
// only valid during the call — copy if retained. The orchestrator turns this into the relocate request.
typedef void (*ui_devices_relocate_cb)(const char *device_id, const char *new_area, const char *new_area_name);

// Build/refresh the devices list into `parent` from a parsed /api/v1/rooms response (`root` = the whole
// object with .rooms[]). Clears `parent` first. Must be called under the LVGL lock. `cb` may be NULL.
// A no-op while the room-picker modal is open (so a background refresh can't yank it out from under a tap).
void ui_devices_render(cJSON *root, lv_obj_t *parent, ui_devices_relocate_cb cb);
