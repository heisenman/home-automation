// Devices-management screen (ADR-0026 location / panel-ui-spatial-nav) — an editable table of every
// device: columns Device (name), Location (room), Status (Active/Hidden/Retired). Each cell edits into a
// STAGED change (highlighted); a per-row confirm (checkmark) commits it — so a stray tap never mutates.
//
// Sources: /api/v1/rooms (the located device list + room options) merged with /api/v1/devices/meta (the
// name/hidden/retired overlay, so Status is accurate and hidden/retired devices still appear to be
// reactivated). On commit the orchestrator turns the staged fields into the right writes: Location -> the
// canonical relocate (POST /relocate); Name + Status -> the display overlay (PUT /meta). Rebuilt each render
// (a no-op while an editor modal is open). Fonts compiled: montserrat 14/20/28 only.
#pragma once
#include "lvgl.h"
#include "cJSON.h"

// Status values (map to the meta overlay): Active = shown; Hidden = dropped from views; Retired = decommissioned.
enum { UI_DEV_ACTIVE = 0, UI_DEV_HIDDEN = 1, UI_DEV_RETIRED = 2 };

// Commit callback: the user confirmed a row. Any field not staged is passed "empty": `new_room`/`new_name`
// are NULL when not changed; `new_status` is -1 when not changed. Pointers valid only during the call.
typedef void (*ui_devices_commit_cb)(const char *device_id,
                                     const char *new_room, const char *new_room_name,
                                     const char *new_name, int new_status);

// Build/refresh the table into `parent` from parsed /api/v1/rooms (`rooms_doc`, .rooms[]) + /api/v1/devices/meta
// (`meta_doc`, .meta{}). Clears `parent` first. Must be called under the LVGL lock. `cb` may be NULL.
void ui_devices_render(cJSON *rooms_doc, cJSON *meta_doc, lv_obj_t *parent, ui_devices_commit_cb cb);
