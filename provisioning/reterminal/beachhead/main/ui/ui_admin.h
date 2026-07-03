// Admin session + transient status line — extracted from ui_tiles.c (ADR-0020 module-first).
//
// Owns the RAM-only admin JWT (minted by /auth/login from a typed passphrase), the gear
// lock/unlock button + password keyboard overlay, and the admin request worker that performs
// scene/override/policy edits off the click stack. Locked = operator PANEL_TOKEN only (view +
// basic on/off); unlocked = admin edits until idle auto-lock. Also owns the shared toast line
// (ui_toast) that every module reports through. Deps: ui_http (send_json/base), mbedtls (sha256).
#pragma once
#include <stdbool.h>
#include "lvgl.h"

// Build the toast line + gear button into `topbar`, the password keyboard overlay on the top
// layer, and start the admin worker + its queue. Called once by the orchestrator under the LVGL
// lock, after ui_scenes_init has created the scene box (so toast/gear sit to its right).
void ui_admin_init(lv_obj_t *topbar);

// Transient status line in the top bar (self-locks the LVGL mutex). The shared reporting channel
// for scenes/controls/admin — a no-op before ui_admin_init.
void ui_toast(const char *msg);

// True while an admin JWT is held (scene/override/policy edits allowed). Read from the click ctx.
bool ui_admin_active(void);

// Drop the admin session if it has been idle past ADMIN_IDLE_US (relative esp_timer, no wall
// clock). Called each fetch from ui_task.
void ui_admin_check_idle(void);

// Typed admin submitters — enqueue a request onto the admin worker (HTTP off the click stack).
// No-ops (with an "unlock first" toast from the worker) unless an admin JWT is held.
void ui_admin_set_scene(const char *scene);              // POST /control/house/scene
void ui_admin_set_override(const char *id, const char *body);  // POST /control/<id>/override
void ui_admin_set_policy(const char *id, const char *body);    // PUT  /control/<id>/policy
