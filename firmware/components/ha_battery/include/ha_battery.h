// Battery gauge + thermal-gated charging (ADR-0020).
//
// Fuel-gauge-less battery support for panel-class nodes: ADC voltage → SoC (LUT), board temp
// via an IMU, and a charge manager that enables charging only in a safe window and kicks a
// charger IC that has latched "done" while the cell is still low. All board-specific wiring —
// ADC channels, the divider, the PCA9535 expander handle + charge/read-enable pins, the IMU on
// its I2C bus, status GPIOs, the LUT and thresholds — is `ha_battery_cfg_t`, so a new node
// supplies a preset instead of forking. `ha_battery_d1001_cfg()` is the reTerminal D1001 preset.
#pragma once
#include "esp_err.h"
#include <stdbool.h>
#include <stddef.h>
#include "esp_io_expander.h"
#include "driver/i2c_master.h"
#include "ha_battery_profile.h"   // state-normalized V→SoC profile (ADR-0024 §5)

typedef struct {
    // --- ADC voltage sense ---
    int adc_unit;            // ADC_UNIT_1
    int adc_ch_batt;         // battery channel (D1001: ADC_CHANNEL_2)
    int adc_ch_usb;          // USB/VSYS channel, -1 if none (D1001: ADC_CHANNEL_1)
    int adc_atten;           // ADC_ATTEN_DB_12
    int divider_mul;         // on-board divider: mv = cali_mv * divider_mul (D1001: 2)

    // --- charge/read-enable via a PCA9535 expander (optional) ---
    esp_io_expander_handle_t io_expander;   // NULL if none
    uint32_t exp_read_en_mask;   // asserts the sense divider (D1001: 1<<6, active-high)
    uint32_t exp_charge_en_mask; // enables the charger  (D1001: 1<<10, active-LOW)

    // --- status GPIOs ---
    int gpio_charge;         // charge-status, active-low (D1001: 15); -1 if none
    int gpio_vsys_pg;        // VSYS power-good (D1001: 4); -1 if none

    // --- board temperature via an LSM6DS3-class IMU (optional) ---
    i2c_master_bus_handle_t i2c_bus;   // NULL if no temp source
    uint8_t imu_addr;        // D1001: 0x6A

    // --- SoC LUT (21-point mV, 5%/step) — legacy raw-voltage fallback when no profile is set ---
    const int *lut;          // NULL → built-in D1001 LUT
    int lut_n;               // point count (21)

    // --- state-normalized profile (ADR-0024): preferred over the raw LUT above when non-NULL ---
    const ha_batt_profile_t *profile;  // NULL → baked-in ha_batt_profile_d1001_default()
    bool (*display_on_fn)(void);       // display state for normalization; NULL → assume on (base frame)

    // --- charge policy ---
    int usb_present_mv;      // USB rail above this ⇒ cable present (D1001: 4000)
    int volt_high_mv;        // stop charging above this (D1001: 4150)
    int volt_recharge_mv;    // resume charge below this (hysteresis low; D1001: 3800, factory band)
    int temp_min_dc, temp_max_dc;   // safe charge window in deci-°C (D1001: 20..430)
} ha_battery_cfg_t;

typedef struct {
    int  raw_ch2;            // trimmed-avg raw ADC counts (battery)
    int  cali_mv;            // calibrated mV pre-divider
    int  batt_mv;            // battery mV (× divider), instantaneous
    int  batt_mv_smoothed;   // reported/smoothed battery mV
    int  usb_mv;             // USB/VSYS mV; > usb_present_mv ⇒ cable in
    int  vsys_pg;            // VSYS power-good GPIO raw level
    bool charging;           // charge GPIO active-low (low=charging) — charger IC's STAT pin
    bool on_wall;            // external/USB power present (usb_mv > usb_present_mv)
    bool gaining;            // cell is ACTUALLY gaining charge (smoothed mV rising over ~60 s).
                             // Distinct from `charging`: on a current-limited port the STAT pin
                             // can read "charging" while system load holds net cell current ≈ 0.
    int  soc;                // LUT %% from smoothed mV
    int  temp_dc;            // board temp deci-°C, -9999 if unavailable
    bool have_temp;
    bool charge_en;          // charger currently enabled (our commanded state)
} ha_batt_sample_t;

// reTerminal D1001 preset. Inject the shared PCA9535 + I2C-1 handles (owned by the display BSP).
ha_battery_cfg_t ha_battery_d1001_cfg(esp_io_expander_handle_t io_expander,
                                      i2c_master_bus_handle_t i2c_bus);

// --- Charge-manager control (debug/exploration; drive /CE = EN_BAT_CHGn live) ---
// The charge task's automated /CE handling is externally switchable so pin experiments don't fight it.
enum { HA_CHG_AUTO = 0,  // hysteresis (resume <recharge, stop >high) + re-assert /CE each loop (default)
       HA_CHG_HOLD = 1,  // manager does NOT touch /CE — leave it to manual cmd/exp poking
       HA_CHG_ON   = 2,  // force /CE low (charge enabled) every loop
       HA_CHG_OFF  = 3 };// force /CE high (charge disabled) every loop
void ha_battery_charge_mode(int mode);
int  ha_battery_charge_mode_get(void);
void ha_battery_charge_reset_pulse(int ms);   // one clean /CE-high pulse of `ms`, then resume the mode

esp_err_t ha_battery_init(const ha_battery_cfg_t *cfg);   // once, before sampling
esp_err_t ha_battery_sample(ha_batt_sample_t *out);       // thread-safe (internal mutex)
esp_err_t ha_battery_read(int *soc_pct, float *volts, bool *charging);  // convenience
void      ha_battery_charge_start(void);                  // start the thermal-gated charge task
void      ha_battery_dump(char *out, size_t outlen);      // diagnostic string
