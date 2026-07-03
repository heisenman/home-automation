// Host unit test for the ha_battery_profile core (no ESP deps). Run via ./run.sh, or:
//   cc test/test_ha_battery_profile.c ha_battery_profile.c -Iinclude -o /tmp/t && /tmp/t
#include "ha_battery_profile.h"
#include <stdio.h>
#include <string.h>

static int fails = 0;
static void check(const char *name, int cond) {
    printf("%s  %s\n", cond ? "PASS" : "FAIL", name);
    if (!cond) fails++;
}

int main(void) {
    ha_batt_profile_t p = ha_batt_profile_d1001_default();

    // --- provenance + LUT shape ---
    check("has version", p.version[0] == 'v');
    check("lut_n = 21", p.lut_n == 21);
    check("lut ascending & endpoints", p.lut[0] == 3450 && p.lut[20] == 3860);
    int mono = 1;
    for (int i = 1; i < p.lut_n; i++) if (p.lut[i] <= p.lut[i-1]) mono = 0;
    check("lut strictly monotonic", mono);
    check("floors consistent w/ power_policy preset",
          p.run_floor_mv == 3450 && p.warn_mv == 3520 && p.warn_clear_mv == 3580 &&
          p.boot_gate_mv == 3550 && p.boot_release_mv == 3650);

    // --- normalize: base frame (display-on / battery / not-charging) is a no-op ---
    check("base frame unchanged", ha_batt_profile_normalize(&p, 3700, true, false, false) == 3700);
    // display OFF reads +40 above base -> subtract 40
    check("display-off removes +40", ha_batt_profile_normalize(&p, 3740, false, false, false) == 3700);
    // USB attached reads +128 -> subtract 128
    check("usb removes +128", ha_batt_profile_normalize(&p, 3828, true, true, false) == 3700);
    // charging adds +80 on top of USB -> subtract 128+80
    check("usb+charging removes +208", ha_batt_profile_normalize(&p, 3908, true, true, true) == 3700);
    // all three at once (display off + usb + charging) -> subtract 40+128+80 = 248
    check("all offsets remove +248", ha_batt_profile_normalize(&p, 3948, false, true, true) == 3700);

    // --- soc: clamps + endpoints + interpolation ---
    check("below floor -> 0%", ha_batt_profile_soc(&p, 3400) == 0);
    check("at floor -> 0%", ha_batt_profile_soc(&p, 3450) == 0);
    check("above top -> 100%", ha_batt_profile_soc(&p, 3900) == 100);
    check("at top -> 100%", ha_batt_profile_soc(&p, 3860) == 100);
    // round-trip: every LUT point maps back to its own 5%-step SoC (value-agnostic to the curve).
    int rt = 1;
    for (int k = 0; k < p.lut_n; k++) if (ha_batt_profile_soc(&p, p.lut[k]) != k * 5) rt = 0;
    check("every LUT point round-trips to k*5%", rt);
    // midway between two adjacent LUT points lands strictly between their SoCs.
    int mid = ha_batt_profile_soc(&p, (p.lut[10] + p.lut[11]) / 2);
    check("interpolates between points", mid > 50 && mid < 55);
    // monotonic non-decreasing across the whole range
    int prev = -1, ok = 1;
    for (int mv = 3400; mv <= 3900; mv += 5) {
        int s = ha_batt_profile_soc(&p, mv);
        if (s < prev) ok = 0;
        prev = s;
    }
    check("soc monotonic non-decreasing in mV", ok);

    // --- malformed LUT guard ---
    ha_batt_profile_t bad = p; bad.lut_n = 1;
    check("n<2 -> -1", ha_batt_profile_soc(&bad, 3700) == -1);

    printf(fails ? "\n%d CHECK(S) FAILED\n" : "\nALL CHECKS PASS\n", fails);
    return fails ? 1 : 0;
}
