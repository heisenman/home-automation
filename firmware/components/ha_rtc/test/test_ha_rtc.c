// Host test for the ha_rtc pure register core (no IDF). Build+run: ./run.sh
// (HA_RTC_HOST_TEST + _GNU_SOURCE come from run.sh's cc flags.)
#include "ha_rtc.h"
#include <stdio.h>
#include <string.h>
#include <time.h>

static int fails;
#define CHECK(c) do { if (!(c)) { printf("FAIL %s:%d  %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

int main(void)
{
    // --- BCD primitives ---
    CHECK(ha_rtc_bcd2bin(0x00) == 0);
    CHECK(ha_rtc_bcd2bin(0x59) == 59);
    CHECK(ha_rtc_bcd2bin(0x99) == 99);
    CHECK(ha_rtc_bin2bcd(0)  == 0x00);
    CHECK(ha_rtc_bin2bcd(59) == 0x59);
    CHECK(ha_rtc_bin2bcd(26) == 0x26);
    for (int i = 0; i <= 99; i++) CHECK(ha_rtc_bcd2bin(ha_rtc_bin2bcd(i)) == i);

    // --- decode a real 02h..08h block: 2026-07-04 (Sat=6) 10:56:30, VL clear, C=0 ---
    // regs: [sec, min, hr, day, wday, C|month, year]
    uint8_t regs[7] = { 0x30, 0x56, 0x10, 0x04, 0x06, 0x07, 0x26 };
    struct tm t; bool vl;
    ha_rtc_regs_to_tm(regs, &t, &vl);
    CHECK(!vl);
    CHECK(t.tm_sec == 30 && t.tm_min == 56 && t.tm_hour == 10);
    CHECK(t.tm_mday == 4 && t.tm_wday == 6);
    CHECK(t.tm_mon == 6);            // July, 0-indexed
    CHECK(t.tm_year == 126);         // 2026 - 1900

    // --- VL flag set (bit7 of seconds) => not valid ---
    uint8_t regs_vl[7] = { 0x80 | 0x30, 0x56, 0x10, 0x04, 0x06, 0x07, 0x26 };
    ha_rtc_regs_to_tm(regs_vl, &t, &vl);
    CHECK(vl);
    CHECK(t.tm_sec == 30);           // VL bit must be masked out of the seconds value

    // --- century bit (07h bit7) => 19xx ---
    uint8_t regs_c[7] = { 0x30, 0x56, 0x10, 0x04, 0x06, 0x80 | 0x07, 0x99 };
    ha_rtc_regs_to_tm(regs_c, &t, &vl);
    CHECK(t.tm_year == 99);          // 1999 - 1900

    // --- encode round-trip: tm -> regs -> tm, VL always cleared, century for 20xx = 0 ---
    struct tm src = { .tm_sec = 45, .tm_min = 12, .tm_hour = 23, .tm_mday = 31,
                      .tm_mon = 11, .tm_year = 125, .tm_wday = 3 };   // 2025-12-31 23:12:45 Wed
    uint8_t enc[7]; struct tm back; bool bvl;
    ha_rtc_tm_to_regs(&src, enc);
    CHECK((enc[0] & 0x80) == 0);     // VL cleared on set
    CHECK((enc[5] & 0x80) == 0);     // 20xx -> century bit 0
    ha_rtc_regs_to_tm(enc, &back, &bvl);
    CHECK(!bvl);
    CHECK(back.tm_sec == 45 && back.tm_min == 12 && back.tm_hour == 23);
    CHECK(back.tm_mday == 31 && back.tm_mon == 11 && back.tm_year == 125 && back.tm_wday == 3);

    // --- epoch round-trip through timegm (the SNTP glue will do exactly this) ---
    time_t epoch = 1782215565;       // arbitrary fixed instant (no wall-clock calls in tests)
    struct tm *g = gmtime(&epoch);
    uint8_t e2[7]; struct tm r2; bool v2;
    ha_rtc_tm_to_regs(g, e2);
    ha_rtc_regs_to_tm(e2, &r2, &v2);
    r2.tm_isdst = 0;
    CHECK(timegm(&r2) == epoch);

    printf(fails ? "\n%d CHECK(s) FAILED\n" : "all ha_rtc register tests passed\n", fails);
    return fails ? 1 : 0;
}
