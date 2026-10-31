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

    // Water Leak Detector (passive scan: no service data). Real bench frame, MAC replaced. The node can
    // only guess "meter" (-0.9 C / 0 %) — it must keep the raw bytes for the server to decode the leak.
    uint8_t mfrL[19] = {0xaa,0xbb,0xcc,0x00,0x00,0x01, 0x0e,0x29,0xf9,0x00,0x00,0x02,0x74,0x3c,0x05,0,0,0,0};
    check("leak frame decodes as meter guess", sb_decode(NULL, 0, mfrL, 19, &r) && r.valid);
    check("leak guess temp=-0.9", fabs(r.temperature_c + 0.9f) < 0.001);
    check("leak raw kept (19 B)", r.raw_mfr_len == 19 && memcmp(r.raw_mfr, mfrL, 19) == 0);
    check("format A keeps no raw", sb_decode(svcA, 6, NULL, 0, &r) && r.raw_mfr_len == 0);

    printf(fails ? "\n%d CHECK(S) FAILED\n" : "\nALL CHECKS PASS\n", fails);
    return fails ? 1 : 0;
}
