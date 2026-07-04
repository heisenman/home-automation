// ha_imu pure conversions — LSM6DS3TR-C register values <-> physical units. No IDF; host-tested.
// Grounded in docs/hardware/lsm6ds3tr-c.pdf: OUT_TEMP 256 LSB/°C @ 25°C zero; ±2g FS = 0.061 mg/LSB;
// WAKE_UP_THS WK_THS[5:0] 1 LSB = FS_XL/2^6 = 2000/64 = 31.25 mg.
#include "ha_imu.h"

int ha_imu_temp_raw_to_dc(int16_t raw)  { return (raw * 10) / 256 + 250; }   // deci-°C
int ha_imu_accel_raw_to_mg(int16_t raw) { return (raw * 61) / 1000; }        // 0.061 mg/LSB

uint8_t ha_imu_wu_ths_from_mg(int mg)
{
    int v = (mg * 32) / 1000;            // 1 LSB = 31.25 mg -> counts = mg / 31.25 = mg*32/1000
    if (v < 1)  v = 1;                   // 0 would disable the threshold; clamp to the smallest real one
    if (v > 63) v = 63;                  // WK_THS is 6 bits
    return (uint8_t)v;
}
