// Host test for ha_aprilaire — frames are the exact bytes captured from our E070 on 2026-10-06.
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ha_aprilaire.h"

static size_t body_of(const char *frame, ha_apr_framer_t *f) {
    memset(f, 0, sizeof *f);
    for (size_t i = 0; frame[i]; i++)
        if (ha_apr_feed(f, (uint8_t)frame[i])) return f->len;
    return 0;
}

int main(void) {
    ha_apr_framer_t f;
    ha_apr_m_t m;

    // live capture with REMOTE on and nobody answering: idle, RH 0x2B = 43 %, code 03 (display E3)
    assert(body_of("\x02M?2B0365\x03", &f) == 8);
    assert(ha_apr_parse_m(f.body, f.len, &m));
    assert(!m.running && m.rh_pct == 43 && m.code == 3);

    // live capture while we answered: code 00
    char ok[16];
    uint8_t cs = ha_apr_checksum((const uint8_t *)"M?2C00", 6);
    snprintf(ok, sizeof ok, "\x02M?2C00%02X\x03", cs);
    assert(body_of(ok, &f) == 8 && ha_apr_parse_m(f.body, f.len, &m));
    assert(!m.running && m.rh_pct == 44 && m.code == 0);

    // corrupted checksum and non-M frames are rejected
    assert(body_of("\x02M?2B0366\x03", &f) == 8 && !ha_apr_parse_m(f.body, f.len, &m));
    assert(body_of("\x02R000401F4010065\x03", &f) && !ha_apr_parse_m(f.body, f.len, &m));

    // garbage before STX is ignored; an overlong body is dropped and the framer resyncs
    memset(&f, 0, sizeof f);
    const char *noisy = "\x00\xFF\x02" "0123456789012345678901234567890123456789" "\x03\x02M?2B0365\x03";
    int done = 0;
    for (size_t i = 0; i < 46 + 11; i++)
        if (ha_apr_feed(&f, (uint8_t)noisy[i])) { done++; assert(ha_apr_parse_m(f.body, f.len, &m)); }
    assert(done == 1);

    // R-frame: exactly what the E070 accepted live (on=0, dryness 4, rh 50.0 %)
    uint8_t r[24];
    size_t n = ha_apr_build_r(&(ha_apr_r_t){ .on = false, .dryness = 4, .rh_x10 = 500 }, r, sizeof r);
    assert(n == 17 && r[0] == 0x02 && r[16] == 0x03);
    assert(memcmp(r + 1, "R000401F40100", 13) == 0);
    assert(ha_apr_checksum(r + 1, 13) == (uint8_t)strtol((char[]){(char)r[14], (char)r[15], 0}, NULL, 16));

    // call = on, dryness 7
    n = ha_apr_build_r(&(ha_apr_r_t){ .on = true, .dryness = 7, .rh_x10 = 500 }, r, sizeof r);
    assert(n == 17 && memcmp(r + 1, "R010701F40100", 13) == 0);

    // out of range is refused
    assert(ha_apr_build_r(&(ha_apr_r_t){ .on = true, .dryness = 8, .rh_x10 = 500 }, r, sizeof r) == 0);
    assert(ha_apr_build_r(&(ha_apr_r_t){ .on = true, .dryness = 7, .rh_x10 = 500 }, r, 10) == 0);

    puts("ha_aprilaire: all tests passed");
    return 0;
}
