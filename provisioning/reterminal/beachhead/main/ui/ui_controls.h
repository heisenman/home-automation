// Actuator controls — extracted from ui_tiles.c (ADR-0020 module-first).
//
// Owns the actuator cards (from /api/v1/displays), the full-screen command overlay (operator
// ON/OFF via PANEL_TOKEN + admin-gated manual override + automation editor), and the command
// worker that POSTs device commands off the click stack. Admin edits route through ui_admin's
// typed submitters. All a no-op when no operator token is compiled in. Deps: ui_admin, ui_http.
#pragma once
#include "lvgl.h"
#include "cJSON.h"

// Build the command overlay on the top layer + start the command worker. Called once by the
// orchestrator under the LVGL lock (no-op unless an operator token is compiled in).
void ui_controls_init(void);

// Reset the actuator registry (zero the count). Caller holds the LVGL lock and separately
// lv_obj_clean()s the shared grid container (render()).
void ui_controls_reset(void);

// Build one actuator card into `grid` from a /api/v1/displays entry, registering it (with its
// current automation policy) so a tap opens the command overlay. Caller holds the LVGL lock.
void ui_controls_add_card(cJSON *d, lv_obj_t *grid);
