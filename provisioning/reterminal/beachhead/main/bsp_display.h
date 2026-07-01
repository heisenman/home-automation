// Lean D1001 display bring-up (ADR-0019 Phase 2).
//   JD9365 800x1280 MIPI-DSI + GSL3670 touch + PCA9535 power rails + LVGL.
//   Replicates Seeed's proven init sequence but drops esp-sr/codec/cam/IMU/RTC
//   (keeps us well under the 4 MB OTA slot). Camera is deliberately left OFF.
//
// Design rule: NON-FATAL. Every step returns an error instead of aborting, so a
// display hiccup can never knock the device off the bus / out of OTA reach
// (bootloader rollback isn't enabled yet — the net/MQTT lifeline must survive).
#pragma once
#include "esp_err.h"
#include "lvgl.h"

// Bring up power rails -> DSI -> panel -> backlight -> LVGL. Returns ESP_OK on a
// lit, LVGL-ready panel. On any failure, logs + returns the error (never aborts).
esp_err_t bsp_display_start(void);

// True once bsp_display_start() has fully succeeded and LVGL is running.
bool bsp_display_ready(void);

// Run `fn(user)` under the LVGL lock (safe LVGL access from other tasks).
// No-op returning false if the display isn't ready.
bool bsp_display_do(void (*fn)(void *user), void *user);

// Convenience: set backlight brightness 0..100%.
esp_err_t bsp_display_brightness(int percent);

// Turn the panel dark cleanly (backlight PWM off + drop the expander backlight/
// display-power rails, which stay off across a CPU reset). Call BEFORE esp_restart
// so an OTA reboot doesn't leave the backlight latched on showing white/garbage.
// Safe no-op if the display was never brought up.
void bsp_display_off(void);
