// BREADCRUMB: firmware/components > ha_cse7766 - CSE7766 power-meter UART frame parser: resync framer, checksum, V/I/P/S/PF, CF-pulse energy with 16-bit wrap. Pure + host-tested; the UART is the caller's. Contract: ADR-0042. Parent: firmware/AGENTS.md.
// REUSE-WHEN: any board with a Chipsea CSE7766 (or HLW8032, same frame) on a UART RX line — Athom/IoTorero
// PG03 plugs, SONOFF S31/POW R2 (stock Tasmota there today). Algorithm follows ESPHome's cse7766 component.
#pragma once
#include <stdbool.h>
#include <stdint.h>

// Wire: 4800 baud 8E1, the chip transmits a 24-byte frame every ~50 ms, unprompted:
//   [0] status  0x55 normal | 0xAA uncalibrated | 0xFx flags (bit3 V cycle over range, bit2 I over range,
//               bit1 power cycle over range = P is 0, bit0 coefficient storage bad)
//   [1] 0x5A
//   [2..4] V coeff  [5..7] V cycle  [8..10] I coeff  [11..13] I cycle  [14..16] P coeff  [17..19] P cycle
//   [20] adj: bit6 V valid, bit5 I valid, bit4 P valid     [21..22] CF pulse counter (16-bit, wraps)
//   [23] checksum = sum of bytes [2..22] mod 256
// V = Vcoeff/Vcycle, I = Icoeff/Icycle, P = Pcoeff/Pcycle (factory-calibrated coefficients live in the chip).
#define HA_CSE_FRAME 24

typedef struct {
    uint8_t buf[HA_CSE_FRAME];
    uint8_t idx;
} ha_cse_framer_t;

// Feed one received byte. Returns true when buf holds a complete frame with a good header and checksum.
// Garbage resyncs on its own. The caller should also reset idx (= 0) after a >500 ms silence.
bool ha_cse_feed(ha_cse_framer_t *f, uint8_t b);

typedef struct {
    // carried between frames
    uint32_t last_power_cycle;   // for holding P through the slow (>1 s) pulse cycles of a light load
    uint16_t cf_last;
    bool     cf_seen;
    uint64_t cf_total;           // pulses since this state was zeroed (16-bit chip counter unwrapped)
    uint32_t power_coeff;        // last P coefficient (energy per pulse)
} ha_cse_state_t;

typedef struct {
    bool  ok;            // false: uncalibrated / chip fault frame — the values below are NOT meaningful
    bool  have_v, have_i, have_p;
    float voltage_v, current_a, power_w;
    float apparent_va;   // V*I when both are present, else 0
    float power_factor;  // P/S clamped to [0,1]; 1 at no load; -1 when not computable
    uint8_t status;      // raw byte [0], for diagnostics
} ha_cse_reading_t;

// Parse a frame the framer accepted. Updates the carried state (energy pulses, held power).
void ha_cse_parse(ha_cse_state_t *st, const uint8_t frame[HA_CSE_FRAME], ha_cse_reading_t *out);

// Energy since the state was zeroed, in Wh (pulses × Pcoeff / 1e6 / 3600 — ESPHome's constant).
double ha_cse_energy_wh(const ha_cse_state_t *st);
