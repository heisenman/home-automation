// Top bar + whole-house scene selector — extracted from ui_tiles.c (ADR-0020 module-first).
//
// Owns the top-bar row container and the Home/Away/Sleep scene buttons (spec-driven from
// /api/v1/house `scenes`, active one highlighted). Setting a scene is admin-gated: taps route
// through ui_admin (unlock prompt when locked). The orchestrator builds the rest of the top-bar
// chrome (toast/battery/gear) into the container this exposes. Deps: ui_admin (active/toast/set).
#pragma once
#include <stddef.h>
#include <stdbool.h>
#include "lvgl.h"
#include "cJSON.h"

// Create the top-bar row (child of `scr`) + the scene box inside it. Called once by the
// orchestrator under the LVGL lock, before ui_admin_init (which appends into the same row).
void ui_scenes_init(lv_obj_t *scr);

// Borrow the top-bar row container so the orchestrator/ui_admin can append their chrome into it.
lv_obj_t *ui_scenes_topbar(void);

// Build the scene buttons once (from `house`.scenes) + refresh the active highlight each fetch.
// Called from ui_task; takes the LVGL lock itself. If the active scene CHANGED since the last
// render, fires the on-change hook (below) AFTER releasing the LVGL lock (so the callback may
// safely drive bsp_display_* / MQTT). The first render (active goes ""->X) counts as a change,
// so the panel adopts the boot scene's policy.
void ui_scenes_render(cJSON *house);

// Register a callback invoked once whenever the active house scene changes (passed the new scene
// name). The composition root wires this to the device-local scene->backlight policy (scene_dim),
// keeping this UI module free of any device-actuation dependency. Pass NULL to clear.
void ui_scenes_set_on_change(void (*cb)(const char *scene));

// Copy the currently-active scene name into `out` (NUL-terminated). Returns true if a scene is known
// yet (a house fetch has landed), false otherwise. Lets a pushed dim table re-apply the live scene now.
bool ui_scenes_active_scene(char *out, size_t len);
