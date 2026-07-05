// Sleep-mode wake overlay (D1001 roadmap #2, device-side pivot).
//
// When the house enters the Sleep scene the panel dims to a faint level (scene_dim, Sleep default low
// but NOT off) and this shows a full-screen dark scrim with a single centered "Wake" button. Tapping
// Wake is a device-LOCAL action: it brightens THIS panel back to the Home level and returns to the
// dashboard, WITHOUT changing the house scene — house-scene changes are admin-gated, and a passer-by
// tapping Wake should just light the panel, not wake the whole house (feedback-cnc-local-settings).
// Physical "real off" stays on the back button (true disp-off); this is the softer Sleep affordance.
//
// Lives on lv_layer_top so it floats above the dashboard and survives screen rebuilds. Created hidden
// once at bring-up, then shown/hidden by the composition root off the scene-change hook.
#pragma once
#include <stdbool.h>
#include "lvgl.h"

// Create the (hidden) overlay on the top layer. Call once after the display/LVGL is up, before the
// tile fetch loop starts, so the first Sleep transition finds it ready. Takes the LVGL lock itself.
void ui_sleep_init(void);

// Show / hide the Sleep overlay. Self-locking; safe to call from a task or an LVGL event callback
// (the port mutex is recursive). No-ops until ui_sleep_init has run.
void ui_sleep_show(void);
void ui_sleep_hide(void);

// True while the Sleep overlay is currently shown.
bool ui_sleep_visible(void);

// Register the tap-Wake handler. On a Wake tap the overlay hides itself, THEN invokes this callback
// (the composition root wires it to the local brighten-to-Home). Pass NULL to clear.
void ui_sleep_set_on_wake(void (*cb)(void));
