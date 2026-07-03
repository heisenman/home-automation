// SGP30 eCO2 + TVOC gas sensor — minimal ESP-IDF (new i2c_master) driver.
//
// Shared firmware component (ADR-0020): pure I2C transport, no secrets, no node policy. Reusable by any
// device with an I2C bus (edge c3/c6/s3, panel, ...). Unlike the SGP40 (which emits a raw SRAW signal you
// feed to the `sensirion_gas_index` component), the SGP30 computes eCO2 (ppm) + TVOC (ppb) ON-CHIP — no
// companion algorithm component is needed.
//
// Sensor: Sensirion SGP30, I2C addr 0x58 (cf. SGP40 @ 0x59). 16-bit big-endian commands; each 16-bit data
// word is followed by CRC-8 (poly 0x31, init 0xFF — same as SGP40). Operation: Init_air_quality once, then
// Measure_air_quality at ~1 Hz. The first ~15 reads after init are a fixed 400 ppm / 0 ppb warmup; the
// dynamic baseline takes ~12 h to fully settle (persist it via get/set_baseline to skip the re-learn).
#pragma once
#include <stdint.h>
#include "esp_err.h"
#include "driver/i2c_master.h"

#define SGP30_I2C_ADDR 0x58

typedef struct {
    i2c_master_dev_handle_t dev;
} sgp30_t;

// Add the SGP30 on an existing I2C master bus AND send Init_air_quality (starts the 1 Hz algorithm).
esp_err_t sgp30_init(sgp30_t *s, i2c_master_bus_handle_t bus, uint32_t scl_hz);

// On-chip self-test (~220 ms). ESP_OK iff the sensor reports all-pass (0xD400). Also a wiring check:
// returns an I2C error (timeout / ESP_ERR_INVALID_STATE) if SDA/SCL/VCC aren't connected. NOTE:
// measure_test perturbs the air-quality algorithm — run it BEFORE sgp30_init, not between live measures.
esp_err_t sgp30_self_test(sgp30_t *s);

// One air-quality measurement (~12 ms): eCO2 (ppm, 400..60000) + TVOC (ppb, 0..60000). MUST be called at
// ~1 Hz for the dynamic baseline to track. The first ~15 reads after init return the fixed 400/0 warmup.
esp_err_t sgp30_measure(sgp30_t *s, uint16_t *eco2_ppm, uint16_t *tvoc_ppb);

// Read / restore the algorithm baseline (correction words) — persist across reboots to skip the ~12 h
// re-learn. Sensirion: only restore a value stored within the last week, else discard it.
esp_err_t sgp30_get_baseline(sgp30_t *s, uint16_t *eco2_base, uint16_t *tvoc_base);
esp_err_t sgp30_set_baseline(sgp30_t *s, uint16_t eco2_base, uint16_t tvoc_base);

// 48-bit serial number (6 bytes), optional identity read.
esp_err_t sgp30_serial(sgp30_t *s, uint8_t serial[6]);
