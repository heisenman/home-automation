// SGP41 VOC + NOx gas sensor — minimal ESP-IDF (new i2c_master) driver.
//
// Shared firmware component (ADR-0020): pure I2C transport, no secrets, no node policy. Reusable by any
// device that has an I2C bus. Pair it with the `sensirion_gas_index` component TWICE — once as
// ALGORITHM_TYPE_VOC and once as ALGORITHM_TYPE_NOX — to turn the two raw signals into indices.
//
// Sensor: Sensirion SGP41, I2C addr 0x59, commands are 16-bit big-endian, each 16-bit data word is
// followed by a CRC-8 (poly 0x31, init 0xFF). Operation = 10 s conditioning, then continuous 1 Hz
// measure_raw_signals.
//
// WHY THIS COMPONENT EXISTS SEPARATELY FROM sgp40, AND WHY sgp4x_identify() IS THE POINT
// ------------------------------------------------------------------------------------
// The SGP41 is the SGP40's pin-, package- AND ADDRESS-compatible successor: both ACK at 0x59 and both
// pass the same self-test. A bus scan therefore sees one address for two DIFFERENT command sets —
// measure_raw is 0x260F on the SGP40 and 0x2619 on the SGP41, and neither part implements the other's.
// So an address probe cannot tell them apart, and a module silkscreened one way may carry the other.
// sgp4x_identify() is the disambiguator; ha_gas calls it instead of trusting the probe.
//
// BREADCRUMB: firmware/components > sgp41 - Sensirion SGP41 VOC+NOx I2C driver, plus sgp4x_identify() which tells an SGP41 from an SGP40 behaviourally (they share address 0x59, so a bus scan cannot). Contract: ADR-0020. Parent: firmware/AGENTS.md.
// REUSE-WHEN: a node has an SGP4x on its I2C bus — always route through sgp4x_identify() rather than assuming the part, and reach here (not sgp40) when you need the NOx pixel.
#pragma once
#include <stdint.h>
#include "esp_err.h"
#include "driver/i2c_master.h"

#define SGP41_I2C_ADDR   0x59     // shared with the SGP40 — see sgp4x_identify()
#define SGP41_DEFAULT_RH 0x8000   // 50 %RH  — uncompensated default (datasheet Table 12)
#define SGP41_DEFAULT_T  0x6666   // 25 °C   — uncompensated default (datasheet Table 12)

typedef struct {
    i2c_master_dev_handle_t dev;
} sgp41_t;

// Which member of the SGP4x family is actually on the bus at 0x59.
typedef enum {
    SGP4X_PART_UNKNOWN = 0,   // something ACKs at 0x59 but answers to neither command set
    SGP4X_PART_SGP40,         // VOC only
    SGP4X_PART_SGP41,         // VOC + NOx
} sgp4x_part_t;

// Identify the part at 0x59 — the whole reason this component is split out.
//
// Method, in priority order, and why:
//   1. BEHAVIOURAL (authoritative). Issue the SGP41's measure_raw_signals (0x2619) and require an ACK
//      plus two CRC-valid words. That command is documented for the SGP41 and absent from the SGP40's
//      command table, so a part that answers it correctly is an SGP41. If it doesn't, try the SGP40's
//      measure_raw (0x260F) the same way.
//   2. FEATURESET (corroborating only, logged). get_featureset (0x202F) with the low 9 bits masked is
//      0x0020 on an SGP40 and 0x0040 on an SGP41. It is what most libraries use — but it is documented
//      in NEITHER datasheet's command table, and newer die revisions have shipped values that broke
//      exact-match checks in the wild (esphome/issues#5995). Hence: informative, not decisive.
//
// `featureset_out` (optional) receives the raw 0x202F word, or 0 if that read failed — worth logging,
// because it is the breadcrumb if a future revision confuses the behavioural probe too.
//
// Leaves the sensor in IDLE (the probes switch the hotplate on; this turns it back off) so the caller
// can run the conditioning sequence from the state the datasheet requires. Safe to call before
// sgp41_init / sgp40_init; it adds and removes its own device handle on the caller's bus.
sgp4x_part_t sgp4x_identify(i2c_master_bus_handle_t bus, uint32_t scl_hz, uint16_t *featureset_out);

// Add the SGP41 as a device on an already-created I2C master bus.
esp_err_t sgp41_init(sgp41_t *s, i2c_master_bus_handle_t bus, uint32_t scl_hz);

// On-chip self-test of BOTH hotplates (~320 ms). ESP_OK iff both pixels pass.
//
// NB the pass condition is a MASK, not an equality. The datasheet (Table 15) says to ignore the most
// significant byte and read only the low nibble of the low byte: bit 0 = VOC pixel, bit 1 = NOx pixel,
// 0 = passed. The SGP40's documented all-pass word 0xD400 satisfies that mask too, but checking for
// 0xD400 exactly — as several libraries do — is stricter than the SGP41 spec allows.
//
// Doubles as a wiring check: returns an I2C error if SDA/SCL/VCC aren't connected.
// `result_out` (optional) receives the raw word, so a partial failure can name the dead pixel.
esp_err_t sgp41_self_test(sgp41_t *s, uint16_t *result_out);

// Conditioning (0x2612, ~50 ms). MUST be called from idle before the first measure_raw_signals after
// any restart or heater-off: it runs the NOx pixel at a different temperature to bring it up to
// operating condition. Call it at 1 Hz for 10 s — the datasheet says 10 s is recommended and that 10 s
// MUST NOT be exceeded, so this is a bounded phase, not a warm-up you can leave running.
//
// Takes the same RH/T parameter block as the measurement (it is not a bare command) and returns the VOC
// pixel's raw signal, which is already valid during conditioning — so the VOC index algorithm can be fed
// from the first second rather than idling for 10 s. `sraw_voc_out` may be NULL.
esp_err_t sgp41_condition(sgp41_t *s, uint16_t rh_ticks, uint16_t t_ticks, uint16_t *sraw_voc_out);

// One raw VOC+NOx measurement (~50 ms) with humidity/temperature compensation. Pass SGP41_DEFAULT_RH/_T
// for uncompensated, or convert real values with sgp41_rh_ticks()/sgp41_t_ticks(). Returns the two raw
// signals in datasheet order (VOC first, then NOx). Either out-pointer may be NULL.
esp_err_t sgp41_measure_raw(sgp41_t *s, uint16_t rh_ticks, uint16_t t_ticks,
                            uint16_t *sraw_voc_out, uint16_t *sraw_nox_out);

// Idle both hotplates (0x3615) and return to idle mode.
esp_err_t sgp41_heater_off(sgp41_t *s);

// 48-bit serial number (6 bytes), optional identity read.
esp_err_t sgp41_serial(sgp41_t *s, uint8_t serial[6]);

// Real RH% / T°C -> compensation ticks. Identical formulas to the SGP40's (SGP41 datasheet Table 12
// matches SGP40 Table 10); duplicated rather than cross-included so each driver stays a standalone,
// dependency-free transport component.
static inline uint16_t sgp41_rh_ticks(float rh_pct) {
    if (rh_pct < 0.f) rh_pct = 0.f; else if (rh_pct > 100.f) rh_pct = 100.f;
    return (uint16_t)(rh_pct * 65535.f / 100.f + 0.5f);
}
static inline uint16_t sgp41_t_ticks(float t_c) {
    if (t_c < -45.f) t_c = -45.f; else if (t_c > 130.f) t_c = 130.f;
    return (uint16_t)((t_c + 45.f) * 65535.f / 175.f + 0.5f);
}
