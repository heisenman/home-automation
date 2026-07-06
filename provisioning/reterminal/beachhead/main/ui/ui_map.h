// House map (ADR-0019 / panel-ui-spatial-nav) — the house→room→device landing screen.
//
// Renders GET /api/v1/rooms (the canonical room graph: id/name/level/zone + geometry + devices
// grouped + counts) into a spatial layout: one chip per canonical area, POSITIONED at the room's
// real geometry centroid (house-space coords scaled to the screen), showing name + a glance reading
// + a device count. Rooms without a polygon (monolithic levels: attic/crawlspace) render in a strip
// along the bottom. Tapping a room invokes the supplied callback with its area id.
//
// Increment 1 (this module): positioned chips over a plain ground — the existing lv_obj/label infra,
// no lv_canvas (keeps the PSRAM draw budget untouched, per the display-fix lesson). Increment 2 will
// add true polygon walls via lv_canvas once budgeted.
#pragma once
#include "lvgl.h"
#include "cJSON.h"

// Tap callback: the tapped room's canonical area id + display name (stable pointers only valid during
// the call — copy if retained). Used by the orchestrator to open the room-zoom view + title it.
typedef void (*ui_map_room_cb)(const char *area_id, const char *name);

// Build/refresh the house map into `parent` from a parsed /api/v1/rooms response (`root` = the whole
// object with .rooms[]). Clears `parent` first. Must be called under the LVGL lock. `cb` may be NULL.
void ui_map_render(cJSON *root, lv_obj_t *parent, ui_map_room_cb cb);
