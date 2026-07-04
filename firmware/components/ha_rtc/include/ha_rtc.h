// BREADCRUMB: firmware/components > ha_rtc - PCF8563 real-time-clock driver: wall-clock read/set + a valid-time gate, board I2C injected. Contract: ADR-0019. Parent: firmware/AGENTS.md.
// REUSE-WHEN: a panel needs a hardware wall clock that survives reboots (holdover) and a "is the time trustworthy yet?" signal, off an I2C PCF8563/PCF8564-class RTC
//
// ha_rtc — NXP PCF8563 real-time clock/calendar (D1001 U20, I2C1 @ 0x51). Implements ability G
// (wall clock) from docs/design/d1001-capability-roadmap.md. Grounded in docs/hardware/PCF8563.pdf:
// BCD time registers 02h..08h, the VL (voltage-low) flag at 02h bit7 = "oscillator integrity lost
// since the last set → time is untrustworthy", and the STOP bit (00h bit5) held during a write.
//
// Split: the register<->struct-tm conversions are PURE (ha_rtc_regs.c, host-tested, no IDF); the I2C
// transport (ha_rtc.c) takes an injected i2c_master bus + address so the board BSP owns the pins.
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <time.h>

#define HA_RTC_PCF8563_ADDR  0x51   // 7-bit; datasheet write A2h/read A3h (PCF8563.pdf §9.5.1)
#define HA_RTC_REG_VL_SECS   0x02   // first time register; a burst read of 7 from here is coherent
#define HA_RTC_NREGS         7      // VL_seconds, minutes, hours, days, weekdays, century_months, years

// --- Pure register core (ha_rtc_regs.c) — host-testable, no IDF. regs[] is the raw 02h..08h block. ---

uint8_t ha_rtc_bcd2bin(uint8_t bcd);
uint8_t ha_rtc_bin2bcd(uint8_t bin);

// Decode a raw 02h..08h register block into a struct tm (tm_year = years since 1900, tm_mon = 0..11,
// tm_wday = 0..6). `*vl_out` receives the voltage-low flag (true = time NOT trustworthy). Century is
// read from 07h bit7 (C=0 → 20xx, C=1 → 19xx). Pure.
void ha_rtc_regs_to_tm(const uint8_t regs[HA_RTC_NREGS], struct tm *out, bool *vl_out);

// Encode a struct tm into a raw 02h..08h block ready to write: BCD fields, VL cleared, century bit set
// from tm_year (>=2000 → C=0). tm_wday is written as-is (0..6). Pure.
void ha_rtc_tm_to_regs(const struct tm *in, uint8_t regs[HA_RTC_NREGS]);

// --- I2C transport (ha_rtc.c) — needs ESP-IDF; the board injects the bus + address. ---
#ifndef HA_RTC_HOST_TEST
#include "driver/i2c_master.h"

typedef struct {
    i2c_master_bus_handle_t bus;   // the board's already-initialized I2C bus (D1001: bsp_i2c1())
    uint8_t addr;                  // 7-bit device address (HA_RTC_PCF8563_ADDR unless a board differs)
} ha_rtc_cfg_t;

// Attach the RTC to the injected bus. Idempotent. Returns ESP_OK once the device handle is ready.
esp_err_t ha_rtc_init(const ha_rtc_cfg_t *cfg);

// Is the RTC responding on the bus? (i2c probe) — call after init.
bool ha_rtc_present(void);

// Coherent burst-read of the time (registers freeze during the read). `*valid_out` = !VL, i.e. false
// if the RTC lost time since it was last set — the caller MUST NOT display the clock until valid.
esp_err_t ha_rtc_get(struct tm *out, bool *valid_out);

// Set the time (holds STOP across the write so no carry corrupts it) and clear VL. Call this from the
// SNTP-synced hook so the RTC becomes the reboot-holdover source.
esp_err_t ha_rtc_set(const struct tm *in);
#endif // HA_RTC_HOST_TEST
