// BREADCRUMB: firmware/components > ha_imu - LSM6DS3TR-C IMU: temp + accel + hardware wake/tap engine, board I2C injected. Contract: ADR-0019. Parent: firmware/AGENTS.md.
// REUSE-WHEN: a node has the on-board 6-axis IMU and needs board temperature (thermal gating), raw accel, or hardware motion/tap detection (presence, tap-to-wake)
//
// ha_imu — STMicro LSM6DS3TR-C (D1001 U20, I2C1 @ 0x6A; 6D_INTn wired to the P4). Owns the ONE
// physical chip so consumers COMPOSE rather than fight over CTRL1_XL: `ha_battery` reads board temp
// through it (thermal-gated charge, ADR-0024) and the panel presence/tap-to-wake features drive the
// wake engine (roadmap #3, ability A). See README.md "Composition" for how the two components fit.
//
// Grounded verbatim in docs/hardware/lsm6ds3tr-c.pdf. Pure conversions (ha_imu_regs.c) are host-tested.
#pragma once
#include <stdbool.h>
#include <stdint.h>

#define HA_IMU_LSM6_ADDR   0x6A   // 7-bit I2C; WHO_AM_I (0x0F) also fixed at 0x6A (datasheet §9.12)

// --- pure conversions (ha_imu_regs.c) — host-testable, no IDF ---
int     ha_imu_temp_raw_to_dc(int16_t raw);   // OUT_TEMP -> deci-°C (25.0°C + raw/256; 256 LSB/°C)
int     ha_imu_accel_raw_to_mg(int16_t raw);  // OUT_*_XL -> milli-g at ±2g FS (0.061 mg/LSB)
uint8_t ha_imu_wu_ths_from_mg(int mg);        // wake threshold mg -> WK_THS[5:0] (1 LSB = FS/64 = 31.25 mg)

#ifndef HA_IMU_HOST_TEST
#include "driver/i2c_master.h"

typedef struct {
    i2c_master_bus_handle_t bus;   // the board's I2C bus (D1001: bsp_i2c1())
    uint8_t addr;                  // 7-bit address (HA_IMU_LSM6_ADDR unless a board differs)
} ha_imu_cfg_t;

// Attach + configure the IMU. IDEMPOTENT (one physical chip, one config) — safe to call from every
// consumer. Verifies WHO_AM_I, sets CTRL3_C (BDU + IF_INC) and CTRL1_XL (26 Hz, ±2g). ESP_OK on success.
esp_err_t ha_imu_init(const ha_imu_cfg_t *cfg);
bool      ha_imu_present(void);

esp_err_t ha_imu_temp_dc(int *dc);                 // board temp in deci-°C (consumed by ha_battery)
esp_err_t ha_imu_accel_mg(int *x, int *y, int *z); // per-axis acceleration in milli-g

// Enable the hardware wake-up (activity) + single-tap engine, latched, routed to INT1 (6D_INTn).
// `wake_ths_mg` ~= how hard a motion counts as presence (≈100–200 mg is a good approach threshold).
esp_err_t ha_imu_events_enable(int wake_ths_mg);
// Poll the event source registers: *motion = WAKE_UP_SRC.WU_IA, *tap = TAP_SRC single-tap. Reading
// clears the latch (LIR). Cheap; call on a slow cadence or gate it behind the 6D_INTn interrupt.
esp_err_t ha_imu_poll(bool *motion, bool *tap);
#endif // HA_IMU_HOST_TEST
