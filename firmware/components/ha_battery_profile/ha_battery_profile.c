// ha_battery_profile — pure core (ADR-0024 §5). No ESP deps; compiled by the host test too.
#include "ha_battery_profile.h"
#include <string.h>

ha_batt_profile_t ha_batt_profile_d1001_default(void)
{
    ha_batt_profile_t p;
    memset(&p, 0, sizeof(p));
    strcpy(p.version, "v4");
    strcpy(p.date,    "2026-07-04");
    strcpy(p.method,  "auto-discharge");

    // Measured state offsets (D1001 characterization session, at ~mid SoC). NOTE: these are
    // SoC-DEPENDENT in reality (the USB/load-sag offset shrinks toward full — measured ~52 mV at
    // 100% vs 128 mV mid-range). A single-point measurement gives CONSTANT offsets that cancel
    // state-change jumps in the mid-range (where they were measured) but only approximately near
    // the extremes. Full jump-freedom needs offsets characterized across SoC (the harness run).
    // The top extreme is covered instead by the charge-terminated→100% anchor in ha_battery.
    // CARRIED FROM v3 (this v4 regression re-fit only the LUT from the discharge curve; the state
    // offsets come from transition measurements, not the discharge run, so they are unchanged).
    p.off_display_off_mv = 40;
    p.off_usb_mv         = 128;
    p.off_charging_mv    = 80;

    // v4 REGRESSED LUT — from the full 122-min on-battery discharge (session 39 of the 2026-07-04 SD
    // pull, tools/e1001_profile.py d1001): a GENUINE terminated-100% start (session 38 snapped to 100
    // on charge-term, then discharged from full), fine 2 s cadence, ALL THE WAY to the 3450 policy
    // floor — so 0–100% is fully MEASURED (v3 extrapolated 0–16% and guessed its top anchor). Base
    // frame = on-battery / display-on / USB-out (no offsets to remove); constant panel load => coulombs
    // ~ time. 21 pts @ 5%/step. 100% = 3780 mV = the settled loaded voltage the instant of unplug (raw
    // batt_mv settles to ~3786 within ~10 s; smooth lags and is NOT used); 0% = 3450 = the floor.
    // Measured at the real operating current (this discharge IS the panel running on battery), so the
    // curve sits ~40–80 mV below v3's gentler 146-min run — correcting v3's mid-SoC UNDER-read (a
    // 3624 mV cell read ~45% under v3, ~60% here). Strictly monotonic so SoC never inverts.
    static const int lut21[21] = {
        3450, 3466, 3482, 3498, 3508, 3526, 3540, 3550, 3564, 3578, 3594,
        3611, 3624, 3636, 3644, 3660, 3672, 3688, 3712, 3742, 3780,
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

bool ha_batt_profile_valid(const ha_batt_profile_t *p)
{
    if (!p) return false;
    if (p->version[0] == '\0') return false;                          // must carry provenance
    if (p->lut_n < 2 || p->lut_n > HA_BATT_PROFILE_LUT_MAX) return false;
    for (int i = 1; i < p->lut_n; i++)
        if (p->lut[i] <= p->lut[i - 1]) return false;                 // strictly ascending => SoC never inverts
    if (p->run_floor_mv <= 0) return false;
    if (p->warn_mv < p->run_floor_mv) return false;                   // warn at or above the hard floor
    if (p->warn_clear_mv < p->warn_mv) return false;                  // clear >= entry (hysteresis)
    if (p->boot_release_mv < p->boot_gate_mv) return false;           // release >= gate (hysteresis)
    return true;
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
