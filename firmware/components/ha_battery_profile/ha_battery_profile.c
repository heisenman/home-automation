// ha_battery_profile — pure core (ADR-0024 §5). No ESP deps; compiled by the host test too.
#include "ha_battery_profile.h"
#include <string.h>

ha_batt_profile_t ha_batt_profile_d1001_default(void)
{
    ha_batt_profile_t p;
    memset(&p, 0, sizeof(p));
    strcpy(p.version, "v3");
    strcpy(p.date,    "2026-07-03");
    strcpy(p.method,  "bench-discharge");

    // Measured state offsets (D1001 characterization session, at ~mid SoC). NOTE: these are
    // SoC-DEPENDENT in reality (the USB/load-sag offset shrinks toward full — measured ~52 mV at
    // 100% vs 128 mV mid-range). A single-point measurement gives CONSTANT offsets that cancel
    // state-change jumps in the mid-range (where they were measured) but only approximately near
    // the extremes. Full jump-freedom needs offsets characterized across SoC (the harness run).
    // The top extreme is covered instead by the charge-terminated→100% anchor in ha_battery.
    p.off_display_off_mv = 40;
    p.off_usb_mv         = 128;
    p.off_charging_mv    = 80;

    // REGRESSED LUT from the 146-min fixed-load discharge (display-on / USB-out = base frame;
    // constant load => coulombs ~ time). 21 points @ 5%/step. Anchored 100% = 3808 mV — the
    // BASE-FRAME full reading (the cell the instant it was unplugged from a terminated charge), NOT
    // a charging/USB voltage (v2's bug: it used 3860, an on-USB number, which read a full cell as
    // ~72% on battery). The discharge began at full, so ~16–100% (3509–3808 mV) is MEASURED and
    // captures the real Li-ion plateau; only 0–16% (below the safe-stop, down to the 3450 floor) is
    // extrapolated at the end-rate. Strictly monotonic so SoC never inverts.
    static const int lut21[21] = {
        3450, 3469, 3488, 3507, 3526, 3538, 3556, 3576, 3596, 3622, 3642,
        3656, 3702, 3706, 3727, 3738, 3750, 3765, 3781, 3800, 3808,
    };
    p.lut_n = 21;
    memcpy(p.lut, lut21, sizeof(lut21));

    // Safety floors (base frame) — must stay consistent with ha_power_policy_d1001_cfg().
    p.run_floor_mv    = 3450;
    p.warn_mv         = 3520;
    p.warn_clear_mv   = 3580;
    p.boot_gate_mv    = 3550;
    p.boot_release_mv = 3650;
    return p;
}

int ha_batt_profile_normalize(const ha_batt_profile_t *p, int v_meas,
                              bool display_on, bool on_usb, bool charging)
{
    int v = v_meas;
    if (!display_on) v -= p->off_display_off_mv;  // remove the no-load (display-off) boost
    if (on_usb)      v -= p->off_usb_mv;          // remove the USB-attached I·R step
    if (charging)    v -= p->off_charging_mv;     // remove the charge-terminal elevation
    return v;
}

int ha_batt_profile_soc(const ha_batt_profile_t *p, int v_norm)
{
    const int *L = p->lut;
    int n = p->lut_n;
    if (!L || n < 2) return -1;
    if (v_norm <= L[0])   return 0;
    if (v_norm >= L[n-1]) return 100;
    for (int i = 1; i < n; i++) {
        if (v_norm < L[i]) {
            int lo = L[i - 1], hi = L[i];
            int pct_lo = (i - 1) * 100 / (n - 1);
            int pct_hi =  i      * 100 / (n - 1);
            if (hi == lo) return pct_lo;                 // guard a flat LUT segment
            return pct_lo + (v_norm - lo) * (pct_hi - pct_lo) / (hi - lo);
        }
    }
    return 100;
}
