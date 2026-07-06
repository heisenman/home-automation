// Admin session + transient status line — extracted from ui_tiles.c (ADR-0020 module-first).
//
// D1001 displays are UNLOCKED for now — physical access is trusted (feedback-displays-unlocked): NO
// on-screen lock/gear/keyboard, every control available. The panel still authenticates under the hood,
// holding a RAM-only admin JWT it mints automatically at boot from secrets.h ADMIN_API_TOKEN and
// re-mints on 401/expiry. UI visibility (always on) is decoupled from the JWT. Also owns the shared
// toast line (ui_toast) every module reports through. Deps: ui_http (send_json/base), secrets.h.
#pragma once
#include <stdbool.h>
#include "lvgl.h"

// Build the shared toast line into `topbar` + start the auto-admin worker (mints/refreshes the admin
// JWT with no user interaction) + its queue. Called once by the orchestrator under the LVGL lock,
// after ui_scenes_init has created the scene box (so the toast sits to its right).
void ui_admin_init(lv_obj_t *topbar);

// Transient status line in the top bar (self-locks the LVGL mutex). The shared reporting channel
// for scenes/controls/admin — a no-op before ui_admin_init.
void ui_toast(const char *msg);

// Whether the admin UI is available. Displays are unlocked for now, so this is always true (controls
// always render). Real auth is the JWT the worker manages, deliberately decoupled from this.
bool ui_admin_active(void);

// No-op while displays are unlocked (kept for the ui_task call site; the JWT self-refreshes on 401).
void ui_admin_check_idle(void);

// Typed admin submitters — enqueue a request onto the admin worker (HTTP off the click stack). The
// worker ensures a valid JWT (auto-login) before each action and re-mints + retries once on 401.
void ui_admin_set_scene(const char *scene);              // POST /control/house/scene
void ui_admin_set_override(const char *id, const char *body);  // POST /control/<id>/override
void ui_admin_set_policy(const char *id, const char *body);    // PUT  /control/<id>/policy
void ui_admin_set_relocate(const char *device_id, const char *body);  // POST /api/v1/devices/<id>/relocate
