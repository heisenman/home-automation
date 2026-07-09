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
// object with .rooms[]). Must be called under the LVGL lock. `cb` may be NULL.
// The map keeps two persistent layers under `parent`: a STATIC wall layer (built once) and a value-box
// layer (rebuilt each refresh) — so a periodic refresh no longer tears down the whole-screen walls (the
// 10 s flicker). `nav`=true (view entry — another view may have cleaned/freed `parent`'s children) forces
// a full rebuild and is safe against stale layer pointers; `nav`=false (periodic) reuses the wall layer.
void ui_map_render(cJSON *root, lv_obj_t *parent, ui_map_room_cb cb, bool nav);

// Device tap callback in the spatial room-zoom: tapped device's id + name (pointers valid only during
// the call — copy if retained).
typedef void (*ui_map_device_cb)(const char *device_id, const char *name);

// Spatial room-zoom (arc 3): render ONE room (by area id) — its polygon zoomed to fill `parent`, its
// placed devices (non-null normalized placement, 0..1 of the room bbox) as callouts, and unplaced
// devices in a bottom fallback strip. Returns false if the room has no geometry (caller falls back to
// the tile grid). Clears `parent`; must hold the LVGL lock. `cb` fires on a device tap (may be NULL).
bool ui_map_render_room(cJSON *root, const char *room_id, lv_obj_t *parent, ui_map_device_cb cb);
