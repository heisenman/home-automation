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
#include <stddef.h>
#include "lvgl.h"

// Diagnostic: probe both I2C buses and write ACKing 7-bit addresses to `out`
// (e.g. "i2c0:0x40 i2c1:0x20,0x51,0x62"). Used to identify the battery fuel gauge.
void bsp_i2c_scan(char *out, size_t outlen);

// Call FIRST in app_main (before WiFi): force the panel dark at boot so the power rails
// don't free-run through the bootloader->app window and strobe the screen (photosensitivity
// hazard). GPIO + I2C only, no DSI/LVGL. Idempotent, non-fatal. Panel stays dark until
// bsp_display_start() (cmd/display on).
void bsp_display_predark(void);

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

// Screen on/off toggle for the back button (GPIO3). UNLIKE bsp_display_off(),
// this KEEPS the panel power rail up (only backlight + DSI display-on) so wake is
// instant with NO re-init — LVGL and the panel config stay live. Idempotent.
void bsp_display_sleep(void);   // blank: backlight off + disp-off, rail stays powered
void bsp_display_wake(void);    // relight: disp-on + backlight restored
void bsp_display_toggle(void);  // flip sleep<->wake based on current state
bool bsp_display_is_on(void);   // true if currently lit (false if sleeping or not ready)
