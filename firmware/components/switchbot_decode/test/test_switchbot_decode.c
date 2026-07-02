// Host unit test for the SwitchBot decoder (no ESP deps). Run via ./run.sh, or manually:
//   cc test/test_switchbot_decode.c switchbot_decode.c -Iinclude -lm -o /tmp/t && /tmp/t
#include "switchbot_decode.h"
#include <stdio.h>
#include <string.h>
#include <math.h>

static int fails = 0;
static void check(const char *name, int cond) {
    printf("%s  %s\n", cond ? "PASS" : "FAIL", name);
    if (!cond) fails++;
}

int main(void) {
    sb_reading_t r;

    // Format A — Meter (0x54), 100%, 22.7 °C, 39% : svc after fd3d UUID
    // [model,status,batt,tempfrac,tempint|0x80,hum]
    uint8_t svcA[6] = {0x54, 0x00, 0x64, 0x07, 0x96, 0x27};
    check("A decodes", sb_decode(svcA, 6, NULL, 0, &r) && r.valid);
    check("A temp=22.7", fabs(r.temperature_c - 22.7f) < 0.001);
    check("A hum=39", r.humidity_pct == 39);
    check("A batt=100", r.battery_pct == 100);
    check("A type=meter", strcmp(r.device_type, "switchbot_meter") == 0);

    // Format B — Outdoor (0x77): svc is 3 bytes (battery in svc[2]); temp/hum in mfr after MAC
    uint8_t svcB[3] = {0x77, 0x00, 0x64};
    uint8_t mfrB[11] = {0,0,0,0,0,0, 0x00, 0x00, 0x07, 0x96, 0x27};
    check("B decodes", sb_decode(svcB, 3, mfrB, 11, &r) && r.valid);
    check("B temp=22.7", fabs(r.temperature_c - 22.7f) < 0.001);
    check("B hum=39", r.humidity_pct == 39);
    check("B batt=100", r.battery_pct == 100);
    check("B type=outdoor", strcmp(r.device_type, "switchbot_meter_outdoor") == 0);

    // Negative temp (sign bit clear) -5.3 °C
    uint8_t svcN[6] = {0x54, 0x00, 0x50, 0x03, 0x05, 0x28};
    check("neg temp=-5.3", sb_decode(svcN, 6, NULL, 0, &r) && fabs(r.temperature_c + 5.3f) < 0.001);

    // Out-of-range humidity rejected
    uint8_t svcBad[6] = {0x54, 0x00, 0x64, 0x07, 0x96, 0x65};  // 101%
    check("bad hum rejected", !sb_decode(svcBad, 6, NULL, 0, &r));

    printf(fails ? "\n%d CHECK(S) FAILED\n" : "\nALL CHECKS PASS\n", fails);
    return fails ? 1 : 0;
}
