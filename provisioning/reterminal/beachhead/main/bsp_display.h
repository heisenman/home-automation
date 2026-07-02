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

// Battery (D1001): NO I2C fuel gauge — ADC1 ch2 x2 divider, gated behind PCA9535 pin 6
// (BAT_READ_EN). Fills any non-NULL out param: soc_pct 0..100 (smoothed), volts (smoothed
// cell voltage), charging (GPIO15 active-low). Returns ESP_OK on a good read, else ESP_FAIL.
#include <stdbool.h>
esp_err_t bsp_battery_read(int *soc_pct, float *volts, bool *charging);

// Full instantaneous battery telemetry for ADC profiling (bat_profile.c). All fields raw
// so the discharge log can be re-fit off-line; `soc`/`batt_mv_smoothed` are the reported
// (smoothed) values, everything else is this-instant. Returns ESP_FAIL before the ADC inits.
typedef struct {
    int  raw_ch2;          // trimmed-avg raw ADC counts, battery ch2
    int  cali_mv;          // calibrated mV pre-divider (battery)
    int  batt_mv;          // battery mV (cali x2), instantaneous
    int  batt_mv_smoothed; // reported/smoothed battery mV
    int  usb_mv;           // USB/VSYS mV (ch1 x2) — >~4000 ⇒ charger cable present
    int  vsys_pg;          // GPIO4 raw level (power-good; polarity logged, not interpreted)
    bool charging;         // GPIO15 active-low (low=charging)
    int  soc;              // LUT %% from smoothed mV
    int  temp_dc;          // board temp in deci-°C (LSM6DS3), -9999 if unavailable
    bool have_temp;        // true if temp_dc is valid
    bool charge_en;        // true if the charger is currently enabled (thermal/voltage gated)
} bsp_batt_sample_t;
esp_err_t bsp_battery_sample(bsp_batt_sample_t *out);

// Start the thermal-gated charge manager (idempotent). Charging stays OFF until the cable is
// present, the cell is below full, and the board temp is in a safe window. Fail-safe: no temp
// reading ⇒ no charge. Call once after the display/expander + I2C are available.
void bsp_battery_charge_start(void);

// Diagnostic: dump key MAX17048 registers from 0x36 as hex into `out`
// (e.g. "02=... 04=... 08=..."). VERSION (0x08)=0x001x confirms a real MAX17048.
void bsp_battery_dump(char *out, size_t outlen);

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
