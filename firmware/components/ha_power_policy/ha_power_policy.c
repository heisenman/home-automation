// ha_power_policy — pure decision core (ADR-0024). No ESP deps; compiled by the host test too.
#include "ha_power_policy.h"

ha_power_policy_cfg_t ha_power_policy_d1001_cfg(void)
{
    // v1 provisional — bench characterization 2026-07-03 (docs/design/battery-power-policy.md).
    // Base frame = on-battery / display-on / not-charging. The measured curve is flat
    // (0% ~3450 mV .. ~99% ~3852 mV), so the safety bands sit close together near empty; these
    // are conservative, safety-first, and re-settled by the per-device characterization run.
    return (ha_power_policy_cfg_t){
        .shutdown_mv       = 3450,  // run floor: 0% = safe stop above the knee (Hugh: "not much below 3500")
        .warn_mv           = 3520,  // just above the tested safe-stop (3504) -> warn before the floor
        .warn_clear_mv     = 3580,  // hysteresis so the banner doesn't flap on load ripple
        .boot_gate_mv      = 3550,  // cold-start floor: above the run floor (inrush needs headroom)
        .boot_release_mv   = 3650,  // climb-out: clearly recovering before we power the display
        .shutdown_debounce = 2,     // two consecutive sub-floor samples so one noisy read can't trip it
    };
}

ha_pp_action_t ha_power_policy_eval(const ha_power_policy_cfg_t *cfg,
                                    ha_pp_state_t *st, int norm_mv)
{
    // Shutdown first — it is the highest-priority safety action.
    if (norm_mv <= cfg->shutdown_mv) {
        if (++st->low_streak >= cfg->shutdown_debounce)
            return HA_PP_SHUTDOWN;
        return HA_PP_NONE;          // below floor but not yet debounced
    }
    st->low_streak = 0;             // any sample above the floor breaks the streak

    // Warn band, one-shot on each edge with hysteresis so it can't flap.
    if (!st->warned && norm_mv <= cfg->warn_mv) {
        st->warned = true;
        return HA_PP_WARN_ON;
    }
    if (st->warned && norm_mv >= cfg->warn_clear_mv) {
        st->warned = false;
        return HA_PP_WARN_OFF;
    }
    return HA_PP_NONE;
}

bool ha_power_policy_boot_ok(const ha_power_policy_cfg_t *cfg, int norm_mv, bool was_dark)
{
    // Climbing out of a dark gate needs the higher release threshold; once lit, only a drop
    // below the gate floor matters. This asymmetry stops the display flickering up/down at boot.
    return norm_mv >= (was_dark ? cfg->boot_release_mv : cfg->boot_gate_mv);
}
