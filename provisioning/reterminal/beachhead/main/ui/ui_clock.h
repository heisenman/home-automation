// Top-bar wall clock (D1001 roadmap #1, ability G) — module-first UI element.
//
// Renders local time from the SYSTEM clock, which app_main keeps set from the PCF8563 RTC (reboot
// holdover) + SNTP (see ha_rtc + the clock backbone in beachhead_main.c). Fully decoupled: it only
// reads time(); it shows --:-- until the time is trustworthy (year >= 2020), so no cross-module
// "valid" flag is needed. Mirrors ui_power.
#pragma once
#include "lvgl.h"

// Build the clock label into `topbar` + a 1 Hz refresh timer. Called once by the orchestrator under
// the LVGL lock, after the top-bar row exists.
void ui_clock_init(lv_obj_t *topbar);
