// ha_rtc pure register core — PCF8563 BCD register block <-> struct tm. No IDF; host-tested.
// Grounded in docs/hardware/PCF8563.pdf §8.2 (register organization) — all time fields are BCD, the
// upper "don't care" bits are masked off, VL is 02h bit7, the century flag is 07h bit7.
#include "ha_rtc.h"

uint8_t ha_rtc_bcd2bin(uint8_t bcd) { return (uint8_t)(((bcd >> 4) * 10) + (bcd & 0x0f)); }
uint8_t ha_rtc_bin2bcd(uint8_t bin) { return (uint8_t)(((bin / 10) << 4) | (bin % 10)); }

void ha_rtc_regs_to_tm(const uint8_t regs[HA_RTC_NREGS], struct tm *out, bool *vl_out)
{
    if (vl_out) *vl_out = (regs[0] & 0x80) != 0;      // VL_seconds bit7 — voltage-low / integrity lost
    bool century = (regs[5] & 0x80) != 0;             // century_months bit7 (C=0 -> 20xx, C=1 -> 19xx)

    out->tm_sec  = ha_rtc_bcd2bin(regs[0] & 0x7f);
    out->tm_min  = ha_rtc_bcd2bin(regs[1] & 0x7f);
    out->tm_hour = ha_rtc_bcd2bin(regs[2] & 0x3f);
    out->tm_mday = ha_rtc_bcd2bin(regs[3] & 0x3f);
    out->tm_wday = regs[4] & 0x07;
    out->tm_mon  = ha_rtc_bcd2bin(regs[5] & 0x1f) - 1;          // chip 1..12 -> tm 0..11
    out->tm_year = (century ? 0 : 100) + ha_rtc_bcd2bin(regs[6]); // years since 1900
    out->tm_yday = 0;
    out->tm_isdst = 0;
}

void ha_rtc_tm_to_regs(const struct tm *in, uint8_t regs[HA_RTC_NREGS])
{
    int full_year = in->tm_year + 1900;
    uint8_t century_bit = (full_year >= 2000) ? 0x00 : 0x80;

    regs[0] = ha_rtc_bin2bcd((uint8_t)in->tm_sec) & 0x7f;       // clears VL — the time is now valid
    regs[1] = ha_rtc_bin2bcd((uint8_t)in->tm_min) & 0x7f;
    regs[2] = ha_rtc_bin2bcd((uint8_t)in->tm_hour) & 0x3f;
    regs[3] = ha_rtc_bin2bcd((uint8_t)in->tm_mday) & 0x3f;
    regs[4] = (uint8_t)(in->tm_wday & 0x07);
    regs[5] = (ha_rtc_bin2bcd((uint8_t)(in->tm_mon + 1)) & 0x1f) | century_bit;
    regs[6] = ha_rtc_bin2bcd((uint8_t)(full_year % 100));
}
