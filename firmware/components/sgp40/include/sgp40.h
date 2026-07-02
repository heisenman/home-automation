// SGP40 VOC gas sensor — minimal ESP-IDF (new i2c_master) driver.
//
// Shared firmware component (ADR-0020): pure I2C transport, no secrets, no node policy.
// Reusable by any device that has an I2C bus (edge c3/c6/s3, reTerminal panel, ...). Pair it with
// the `sensirion_gas_index` component to turn the raw SRAW signal into a 0..500 VOC Index.
//
// Sensor: Sensirion SGP40, I2C addr 0x59, commands are 16-bit big-endian, each 16-bit data word is
// followed by a CRC-8 (poly 0x31, init 0xFF). VOC operation = continuous 1 Hz measure_raw.
#pragma once
#include <stdint.h>
#include "esp_err.h"
#include "driver/i2c_master.h"

#define SGP40_I2C_ADDR   0x59
#define SGP40_DEFAULT_RH 0x8000   // 50 %RH  — uncompensated default
#define SGP40_DEFAULT_T  0x6666   // 25 °C   — uncompensated default

typedef struct {
    i2c_master_dev_handle_t dev;
} sgp40_t;

// Add the SGP40 as a device on an already-created I2C master bus.
esp_err_t sgp40_init(sgp40_t *s, i2c_master_bus_handle_t bus, uint32_t scl_hz);

// On-chip self-test (~320 ms). ESP_OK iff the sensor reports all-pass (0xD400). Doubles as a wiring
// check: returns an I2C error (e.g. ESP_ERR_INVALID_STATE / timeout) if SDA/SCL/VCC aren't connected.
esp_err_t sgp40_self_test(sgp40_t *s);

// One raw VOC measurement (~30 ms) with humidity/temperature compensation. Pass SGP40_DEFAULT_RH/_T
// for uncompensated, or convert real values with sgp40_rh_ticks()/sgp40_t_ticks(). Result in *sraw_out.
esp_err_t sgp40_measure_raw(sgp40_t *s, uint16_t rh_ticks, uint16_t t_ticks, uint16_t *sraw_out);

// Idle the hotplate heater (0x3615). Only for low-duty sampling; NOT used in continuous 1 Hz VOC mode.
esp_err_t sgp40_heater_off(sgp40_t *s);

// 48-bit serial number (6 bytes), optional identity read.
esp_err_t sgp40_serial(sgp40_t *s, uint8_t serial[6]);

// Real RH% / T°C -> SGP40 compensation ticks (for measure_raw).
static inline uint16_t sgp40_rh_ticks(float rh_pct) {
    if (rh_pct < 0.f) rh_pct = 0.f; else if (rh_pct > 100.f) rh_pct = 100.f;
    return (uint16_t)(rh_pct * 65535.f / 100.f + 0.5f);
}
static inline uint16_t sgp40_t_ticks(float t_c) {
    if (t_c < -45.f) t_c = -45.f; else if (t_c > 130.f) t_c = 130.f;
    return (uint16_t)((t_c + 45.f) * 65535.f / 175.f + 0.5f);
}
