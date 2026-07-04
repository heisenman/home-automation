// Host test for the ha_imu pure conversions (no IDF). Build+run: ./run.sh
#include "ha_imu.h"
#include <stdio.h>

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

int main(void)
{
    // temp: 256 LSB/°C, 0 LSB = 25.0°C
    CHECK(ha_imu_temp_raw_to_dc(0)     == 250);   // 25.0 °C
    CHECK(ha_imu_temp_raw_to_dc(256)   == 260);   // 26.0 °C
    CHECK(ha_imu_temp_raw_to_dc(-256)  == 240);   // 24.0 °C
    CHECK(ha_imu_temp_raw_to_dc(2560)  == 350);   // 35.0 °C

    // accel: ±2g FS, 0.061 mg/LSB. 1 g ≈ 16384 LSB -> ~999 mg
    CHECK(ha_imu_accel_raw_to_mg(0)      == 0);
    CHECK(ha_imu_accel_raw_to_mg(16384)  == 999);
    CHECK(ha_imu_accel_raw_to_mg(-16384) == -999);

    // wake threshold: 1 LSB = 31.25 mg, clamped [1,63]
    CHECK(ha_imu_wu_ths_from_mg(0)    == 1);      // clamp up (0 would disable)
    CHECK(ha_imu_wu_ths_from_mg(125)  == 4);      // 125/31.25 = 4
    CHECK(ha_imu_wu_ths_from_mg(200)  == 6);      // 200/31.25 = 6.4 -> 6
    CHECK(ha_imu_wu_ths_from_mg(5000) == 63);     // clamp to 6 bits

    printf(fails ? "\n%d CHECK(s) FAILED\n" : "all ha_imu conversion tests passed\n", fails);
    return fails ? 1 : 0;
}
