// Bosch BME680 environmental + gas sensor — minimal ESP-IDF (new i2c_master) driver.
//
// Shared firmware component (ADR-0020): pure I2C transport + the Bosch compensation algorithm baked in,
// no secrets, no node policy. Reusable by any device with an I2C bus.
//
// The BME680 returns RAW ADC counts plus a block of per-chip calibration coefficients; the physical
// values only exist after the vendor compensation math (this file). Temperature/pressure/humidity use
// the datasheet polynomials; gas resistance uses the two Bosch range LUTs. Outputs are real units:
// °C, %RH, hPa, Ω. (Bosch's calibrated IAQ index (BSEC) is a separate closed blob — NOT included; the
// honest air-quality signal here is gas_resistance_ohm, which rises in clean air and falls with VOCs.)
//
// Sensor: BME680, I2C addr 0x76 (SDO->GND) or 0x77 (SDO->VCC), chip id 0x61. Forced-mode single-shot:
// one measure() = configure heater + trigger T/P/H/gas + wait + read + compensate.
#pragma once
#include <stdint.h>
#include "esp_err.h"
#include "driver/i2c_master.h"

#define BME680_I2C_ADDR_PRIMARY   0x76
#define BME680_I2C_ADDR_SECONDARY 0x77
#define BME680_CHIP_ID            0x61

typedef struct {
    i2c_master_dev_handle_t dev;
    // Calibration coefficients (parsed from the on-chip coeff block at init).
    uint16_t par_t1; int16_t par_t2; int8_t par_t3;
    uint16_t par_p1; int16_t par_p2; int8_t  par_p3; int16_t par_p4; int16_t par_p5;
    int8_t   par_p6; int8_t  par_p7; int16_t par_p8; int16_t par_p9; uint8_t par_p10;
    uint16_t par_h1; uint16_t par_h2; int8_t par_h3; int8_t par_h4; int8_t par_h5;
    uint8_t  par_h6; int8_t  par_h7;
    int8_t   par_gh1; int16_t par_gh2; int8_t par_gh3;
    uint8_t  res_heat_range; int8_t res_heat_val; int8_t range_sw_err;
    float    t_fine;          // shared between T and the P/H compensations
    float    amb_temp_c;      // ambient estimate for the heater-resistance calc (updated each read)
} bme680_t;

typedef struct {
    float   temperature_c;
    float   humidity_pct;
    float   pressure_hpa;
    float   gas_resistance_ohm;
    uint8_t gas_valid;        // 1 iff the gas conversion completed AND the heater was stable
} bme680_reading_t;

// Add the BME680 on an already-created I2C master bus, verify chip id, soft-reset, load calibration,
// and program a sane forced-mode profile (T 2x / P 16x / H 1x, gas heater 320 °C for 150 ms). ESP_OK
// iff the sensor answered — doubles as the wiring/presence check (NACK if SDA/SCL/VCC aren't wired).
esp_err_t bme680_init(bme680_t *s, i2c_master_bus_handle_t bus, uint8_t addr, uint32_t scl_hz);

// One forced-mode measurement: trigger, wait out the T/P/H + heater duration, read the field, and
// compensate to physical units in *out. ~200 ms. Returns ESP_OK on a clean I2C exchange (check
// out->gas_valid for the gas lane's heater-stable/converted status).
esp_err_t bme680_measure(bme680_t *s, bme680_reading_t *out);
