// Versioned, loadable battery profile (ADR-0024 §5). The gauge's numbers — state offsets, the
// V→SoC LUT, and the safety floors — are DATA, not compiled constants, so a better curve deploys
// without a firmware reflash and is comparable / rollback-able / improvable in the field.
//
// This header is the pure core: the profile struct + the two math primitives the gauge needs
// (normalize a raw reading into the base frame, then map it to SoC%), plus a baked-in D1001
// default. Load/save (NVS / SD / MQTT-pushed blob) is the runtime side (ha_battery_profile_rt.c),
// which hands back one of these structs; everything here is host-tested and ESP-free.
#pragma once
#include <stdbool.h>

// Base frame (the state in which the gauge is actually read) = on-battery / display-on / not-charging.
// Every other power state reads OFF that frame by a measured offset; normalize removes it before the LUT.
#define HA_BATT_PROFILE_LUT_MAX 32

typedef struct {
    // --- provenance (ADR-0024: every profile is auditable) ---
    char version[16];   // e.g. "v1"
    char date[16];      // "2026-07-03"
    char method[24];    // "bench" | "auto" | "in-field"

    // --- state offsets (mV), each the amount the raw reading sits ABOVE the base frame in that state ---
    int off_display_off_mv;  // display OFF reads higher (no load sag) — D1001 ~+40
    int off_usb_mv;          // USB attached reads higher (I·R under the port) — D1001 ~+128
    int off_charging_mv;     // charging elevates the terminal above OCV — D1001 ~+80

    // --- V→SoC LUT: base-frame mV, ascending, evenly mapped to 0%..100% across lut_n points ---
    int lut[HA_BATT_PROFILE_LUT_MAX];
    int lut_n;

    // --- safety floors (base-frame mV) — the caller builds ha_power_policy_cfg from these ---
    int run_floor_mv;     // hard power-off
    int warn_mv;          // warn band entry
    int warn_clear_mv;    // warn band clear (hysteresis)
    int boot_gate_mv;     // cold-start floor (hold display dark below)
    int boot_release_mv;  // cold-start release (hysteresis)
} ha_batt_profile_t;

// Baked-in D1001 default (v1 — bench 2026-07-03; the LUT is LINEAR-PROVISIONAL between the two
// measured anchors 0%=3450 mV / 100%≈3860 mV, pending the regressed curve from a finer-cadence run).
// This is the fallback when no profile is loaded (ADR-0024 §5).
ha_batt_profile_t ha_batt_profile_d1001_default(void);

// Remove the state offsets: raw ADC mV -> base-frame mV. `display_on`/`on_usb`/`charging` describe
// the state the reading was taken in. Pure; the gauge calls this before the LUT.
int ha_batt_profile_normalize(const ha_batt_profile_t *p, int v_meas,
                              bool display_on, bool on_usb, bool charging);

// Base-frame mV -> SoC %% via linear interpolation over the LUT. Clamps to [0,100]; below lut[0] = 0,
// above lut[n-1] = 100. Returns -1 on a malformed LUT (n < 2). Pure.
int ha_batt_profile_soc(const ha_batt_profile_t *p, int v_norm);
