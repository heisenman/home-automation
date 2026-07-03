// Top-bar power-off button — full hardware shutdown (ADR-0020 module-first).
//
// Adds a POWER button to the top-bar row plus a full-screen confirm overlay. On confirm it
// calls bsp_power_off(), which releases the PWR_HOLD rail latch so the device powers fully
// off (unplugging USB then no longer drains the battery). Deliberately NOT admin-gated:
// anyone with physical access may shut the device down. Power back on is the physical side
// button (a hardware latch, not firmware-reachable) or plugging in USB.
#pragma once
#include "lvgl.h"

// Build the power button into `topbar` + a hidden confirm overlay on the top layer. Called
// once by the orchestrator under the LVGL lock, after the top-bar row exists.
void ui_power_init(lv_obj_t *topbar);
