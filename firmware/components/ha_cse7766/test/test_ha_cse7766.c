// Host test for ha_cse7766. Frames are synthesised from the header's layout; values chosen so V/I/P land
// on round numbers (120 V, 5 A, 600 W) and the energy maths can be checked by hand.
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "ha_cse7766.h"

static void put24(uint8_t *p, uint32_t v) { p[0] = v >> 16; p[1] = v >> 8; p[2] = v; }

static void frame(uint8_t *f, uint8_t status, uint8_t adj, uint32_t vy, uint32_t iy, uint32_t py, uint16_t cf) {
    memset(f, 0, HA_CSE_FRAME);
    f[0] = status; f[1] = 0x5A;
    put24(f + 2, 1200000); put24(f + 5, vy);      // V = 1.2e6 / vy
    put24(f + 8, 50000);   put24(f + 11, iy);     // I = 5e4 / iy
    put24(f + 14, 6000000); put24(f + 17, py);    // P = 6e6 / py
    f[20] = adj; f[21] = cf >> 8; f[22] = cf & 0xFF;
    uint8_t s = 0;
    for (int i = 2; i < 23; i++) s += f[i];
    f[23] = s;
}

static int feed_all(ha_cse_framer_t *fr, const uint8_t *b, int n) {
    int got = 0;
    for (int i = 0; i < n; i++) got += ha_cse_feed(fr, b[i]);
    return got;
}

#define NEAR(a, b) (fabs((double)(a) - (double)(b)) < 1e-3)

int main(void) {
    uint8_t f[HA_CSE_FRAME];
    ha_cse_framer_t fr = {0};
    ha_cse_state_t st = {0};
    ha_cse_reading_t r;

    // a normal frame, all three valid: 120 V, 5 A, 600 W, S 600 VA, PF 1
    frame(f, 0x55, 0x70, 10000, 10000, 10000, 100);
    assert(feed_all(&fr, f, HA_CSE_FRAME) == 1);
    ha_cse_parse(&st, fr.buf, &r);
    assert(r.ok && r.have_v && r.have_i && r.have_p);
    assert(NEAR(r.voltage_v, 120) && NEAR(r.current_a, 5) && NEAR(r.power_w, 600));
    assert(NEAR(r.apparent_va, 600) && NEAR(r.power_factor, 1));

    // leading garbage, a corrupt frame, then a good one: exactly one frame accepted
    uint8_t stream[3 + 2 * HA_CSE_FRAME] = {0x00, 0x13, 0x5A};
    frame(stream + 3, 0x55, 0x70, 10000, 10000, 10000, 100);
    stream[3 + 10] ^= 0xFF;                                  // break the first frame's checksum
    frame(stream + 3 + HA_CSE_FRAME, 0x55, 0x70, 10000, 10000, 10000, 100);
    memset(&fr, 0, sizeof fr);
    assert(feed_all(&fr, stream, sizeof stream) == 1);

    // energy: pulses unwrap across the 16-bit counter (65530 -> 4 = 10 pulses)
    ha_cse_state_t e = {0};
    frame(f, 0x55, 0x70, 10000, 10000, 10000, 65530);
    ha_cse_parse(&e, f, &r);
    frame(f, 0x55, 0x70, 10000, 10000, 10000, 4);
    ha_cse_parse(&e, f, &r);
    assert(e.cf_total == 10);
    assert(NEAR(ha_cse_energy_wh(&e), 10.0 * 6e6 / 1e6 / 3600.0));

    // light load below the 50 mA floor: current reads 0 even though the chip offers a value
    frame(f, 0x55, 0x70, 10000, 10000, 6000000, 0);           // P = 1 W at 120 V -> 8 mA
    ha_cse_parse(&st, f, &r);
    assert(NEAR(r.power_w, 1) && r.current_a == 0.0f && NEAR(r.power_factor, 1) == 0);

    // slow cycle hold: P valid at a 2 s cycle, then a frame without a new pulse inside 2x -> held
    ha_cse_state_t h = {0};
    frame(f, 0x55, 0x70, 10000, 10000, 2000000, 0);           // 3 W, cycle 2e6 µs
    ha_cse_parse(&h, f, &r);
    assert(NEAR(r.power_w, 3));
    frame(f, 0x55, 0x60, 10000, 10000, 3000000, 0);           // no P bit, 3 s since last pulse (< 4 s)
    ha_cse_parse(&h, f, &r);
    assert(r.have_p && NEAR(r.power_w, 3));
    frame(f, 0x55, 0x60, 10000, 10000, 5000000, 0);           // 5 s: overdue -> 0 W
    ha_cse_parse(&h, f, &r);
    assert(r.have_p && r.power_w == 0.0f);

    // "power cycle over range" (0xF2) = 0 W but still a good frame; V/I over range or uncalibrated = not ok
    frame(f, 0xF2, 0x70, 10000, 10000, 10000, 0);
    ha_cse_parse(&st, f, &r);
    assert(r.ok && r.power_w == 0.0f);
    frame(f, 0xF8, 0x70, 10000, 10000, 10000, 0);
    ha_cse_parse(&st, f, &r);
    assert(!r.ok);
    frame(f, 0xAA, 0x70, 10000, 10000, 10000, 0);
    ha_cse_parse(&st, f, &r);
    assert(!r.ok);

    puts("ha_cse7766: all tests passed");
    return 0;
}
