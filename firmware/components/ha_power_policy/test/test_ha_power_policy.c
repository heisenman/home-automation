// Host unit test for the ha_power_policy decision core (no ESP deps). Run via ./run.sh, or:
//   cc test/test_ha_power_policy.c ha_power_policy.c -Iinclude -o /tmp/t && /tmp/t
#include "ha_power_policy.h"
#include <stdio.h>

static int fails = 0;
static void check(const char *name, int cond) {
    printf("%s  %s\n", cond ? "PASS" : "FAIL", name);
    if (!cond) fails++;
}

int main(void) {
    ha_power_policy_cfg_t c = ha_power_policy_d1001_cfg();

    // --- sanity on the preset ordering (safety invariants) ---
    check("cfg shutdown < warn", c.shutdown_mv < c.warn_mv);
    check("cfg warn < warn_clear", c.warn_mv < c.warn_clear_mv);
    check("cfg boot_gate >= shutdown (cold-start needs headroom)", c.boot_gate_mv >= c.shutdown_mv);
    check("cfg boot_release >= boot_gate", c.boot_release_mv >= c.boot_gate_mv);
    check("cfg debounce >= 1", c.shutdown_debounce >= 1);

    // --- eval: healthy cell -> nothing, no warn latched ---
    ha_pp_state_t st = {0};
    check("healthy -> NONE", ha_power_policy_eval(&c, &st, 3800) == HA_PP_NONE);
    check("healthy not warned", !st.warned);

    // --- eval: entering the warn band raises exactly one edge ---
    check("enter warn -> WARN_ON", ha_power_policy_eval(&c, &st, c.warn_mv) == HA_PP_WARN_ON);
    check("still warn -> NONE (one-shot)", ha_power_policy_eval(&c, &st, c.warn_mv - 5) == HA_PP_NONE);
    check("warned latched", st.warned);

    // --- eval: hysteresis — between warn and clear stays warned, no flap ---
    check("mid-band stays NONE", ha_power_policy_eval(&c, &st, c.warn_mv + 20) == HA_PP_NONE);
    check("still warned in hysteresis", st.warned);

    // --- eval: recovery above clear raises WARN_OFF exactly once ---
    check("recover -> WARN_OFF", ha_power_policy_eval(&c, &st, c.warn_clear_mv) == HA_PP_WARN_OFF);
    check("recovered not warned", !st.warned);
    check("stay high -> NONE", ha_power_policy_eval(&c, &st, 3800) == HA_PP_NONE);

    // --- eval: shutdown is debounced (default 2) ---
    ha_pp_state_t s2 = {0};
    check("1st sub-floor -> NONE (debounce)", ha_power_policy_eval(&c, &s2, c.shutdown_mv) == HA_PP_NONE);
    check("2nd sub-floor -> SHUTDOWN", ha_power_policy_eval(&c, &s2, c.shutdown_mv) == HA_PP_SHUTDOWN);

    // --- eval: a single noisy dip does NOT trip (streak resets above floor) ---
    ha_pp_state_t s3 = {0};
    check("noisy dip -> NONE", ha_power_policy_eval(&c, &s3, c.shutdown_mv - 10) == HA_PP_NONE);
    check("recovered read -> NONE", ha_power_policy_eval(&c, &s3, 3700) == HA_PP_NONE);
    check("streak reset", s3.low_streak == 0);
    check("dip again -> NONE (needs 2 in a row)", ha_power_policy_eval(&c, &s3, c.shutdown_mv - 10) == HA_PP_NONE);

    // --- eval: shutdown takes precedence over warn (below floor is not a WARN) ---
    ha_pp_state_t s4 = {0};
    ha_power_policy_eval(&c, &s4, 3800);                 // healthy, not warned
    check("below floor 1st -> NONE not WARN", ha_power_policy_eval(&c, &s4, c.shutdown_mv - 1) == HA_PP_NONE);
    check("below floor not warned", !s4.warned);
    check("below floor 2nd -> SHUTDOWN", ha_power_policy_eval(&c, &s4, c.shutdown_mv - 1) == HA_PP_SHUTDOWN);

    // --- boot_ok: dark climb-out uses the higher release threshold ---
    check("dark, below gate -> not ok", !ha_power_policy_boot_ok(&c, c.boot_gate_mv - 1, true));
    check("dark, between gate&release -> not ok", !ha_power_policy_boot_ok(&c, c.boot_gate_mv, true));
    check("dark, at release -> ok", ha_power_policy_boot_ok(&c, c.boot_release_mv, true));
    // --- boot_ok: once lit, only the gate floor matters (no flicker) ---
    check("lit, above gate -> ok", ha_power_policy_boot_ok(&c, c.boot_gate_mv, false));
    check("lit, below gate -> not ok", !ha_power_policy_boot_ok(&c, c.boot_gate_mv - 1, false));

    printf(fails ? "\n%d CHECK(S) FAILED\n" : "\nALL CHECKS PASS\n", fails);
    return fails ? 1 : 0;
}
