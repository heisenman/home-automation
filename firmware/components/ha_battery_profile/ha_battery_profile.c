// ha_battery_profile — pure core (ADR-0024 §5). No ESP deps; compiled by the host test too.
#include "ha_battery_profile.h"
#include <string.h>

ha_batt_profile_t ha_batt_profile_d1001_default(void)
{
    ha_batt_profile_t p;
    memset(&p, 0, sizeof(p));
    strcpy(p.version, "v1");
    strcpy(p.date,    "2026-07-03");
    strcpy(p.method,  "bench");

    // Measured state offsets (D1001 characterization session).
    p.off_display_off_mv = 40;
    p.off_usb_mv         = 128;
    p.off_charging_mv    = 80;

    // LINEAR-PROVISIONAL LUT between the two measured anchors (0% = 3450 mV, 100% ≈ 3860 mV),
    // 21 points @ 5%/step. Real Li-ion is flat in the middle / steep at the ends — this straight
    // line is a placeholder until the regressed curve from a finer-cadence discharge run replaces
    // it (deploys as data, no reflash, per ADR-0024 §5). Kept monotonic so SoC never inverts.
    static const int lut21[21] = {
        3450, 3471, 3491, 3512, 3532, 3553, 3573, 3594, 3614, 3635, 3655,
        3676, 3696, 3717, 3737, 3758, 3778, 3799, 3819, 3840, 3860,
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
