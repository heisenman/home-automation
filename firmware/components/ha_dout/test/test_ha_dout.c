// Host unit test for ha_dout (no ESP deps). Run via ./run.sh, or manually:
//   cc test/test_ha_dout.c ha_dout.c -Iinclude -o /tmp/t && /tmp/t
#include "ha_dout.h"
#include <stdio.h>

static int fails = 0;
static void check(const char *name, int cond) {
    printf("%s  %s\n", cond ? "PASS" : "FAIL", name);
    if (!cond) fails++;
}

int main(void) {
    ha_dout_t d;

    // ── fail-safe + polarity ───────────────────────────────────────────────────
    ha_dout_cfg_t hi = { .active_high = true };
    ha_dout_init(&d, &hi, 1000);
    check("init is OFF",                 !ha_dout_on(&d));
    check("active_high OFF -> level LOW", !ha_dout_level(&d));
    ha_dout_set(&d, true, 1000);
    check("active_high ON -> level HIGH",  ha_dout_level(&d));

    ha_dout_cfg_t lo = { .active_high = false };
    ha_dout_init(&d, &lo, 1000);
    check("active_low OFF -> level HIGH",  ha_dout_level(&d));
    ha_dout_set(&d, true, 1000);
    check("active_low ON -> level LOW",   !ha_dout_level(&d));

    // An uninitialised struct must still read as the safe level, not as garbage.
    ha_dout_t fresh = {0};
    check("uninit reads OFF",            !ha_dout_on(&fresh));
    check("uninit set is rejected",       ha_dout_set(&fresh, true, 0) == HA_DOUT_REJECTED);

    // ── dwell: a blocked command is deferred, never dropped ────────────────────
    ha_dout_cfg_t dwell = { .active_high = true, .min_on_ms = 5000, .min_off_ms = 3000 };
    ha_dout_init(&d, &dwell, 10000);
    check("ON blocked by min_off",        ha_dout_set(&d, true, 10500) == HA_DOUT_DEFERRED);
    check("  still OFF",                 !ha_dout_on(&d));
    ha_dout_tick(&d, 12000);
    check("  still OFF before dwell",    !ha_dout_on(&d));
    ha_dout_tick(&d, 13000);
    check("  ON once min_off elapsed",    ha_dout_on(&d));

    check("OFF blocked by min_on",        ha_dout_set(&d, false, 14000) == HA_DOUT_DEFERRED);
    check("  still ON",                   ha_dout_on(&d));
    ha_dout_tick(&d, 18000);
    check("  OFF once min_on elapsed",   !ha_dout_on(&d));

    // Re-asserting the current state cancels a pending opposite command.
    ha_dout_init(&d, &dwell, 100000);
    ha_dout_set(&d, true, 104000);                   // applied: min_off satisfied
    check("ON applied",                   ha_dout_on(&d));
    check("OFF deferred",                 ha_dout_set(&d, false, 105000) == HA_DOUT_DEFERRED);
    check("re-ON cancels pending OFF",    ha_dout_set(&d, true, 105500) == HA_DOUT_OK);
    ha_dout_tick(&d, 120000);
    check("  still ON after dwell",       ha_dout_on(&d));

    // ── pulses ────────────────────────────────────────────────────────────────
    ha_dout_cfg_t pulse = { .active_high = true, .max_on_ms = 30000 };
    ha_dout_init(&d, &pulse, 0);
    check("pulse applies",                ha_dout_pulse(&d, 500, 1000) == HA_DOUT_OK);
    check("  ON during pulse",            ha_dout_on(&d));
    ha_dout_tick(&d, 1400);
    check("  still ON before end",        ha_dout_on(&d));
    ha_dout_tick(&d, 1500);
    check("  OFF at end",                !ha_dout_on(&d));

    check("zero-width rejected",          ha_dout_pulse(&d, 0, 2000)     == HA_DOUT_REJECTED);
    check("over max_on rejected",         ha_dout_pulse(&d, 30001, 2000) == HA_DOUT_REJECTED);
    check("at max_on accepted",           ha_dout_pulse(&d, 30000, 2000) == HA_DOUT_OK);

    ha_dout_cfg_t minon = { .active_high = true, .min_on_ms = 200, .max_on_ms = 5000 };
    ha_dout_init(&d, &minon, 0);
    check("pulse under min_on rejected",  ha_dout_pulse(&d, 100, 1000) == HA_DOUT_REJECTED);

    // A level command supersedes an in-flight pulse — the caller wants a state, not a blip.
    ha_dout_init(&d, &pulse, 0);
    ha_dout_pulse(&d, 5000, 1000);
    ha_dout_set(&d, true, 1200);
    ha_dout_tick(&d, 6100);
    check("set() cancels pulse expiry",   ha_dout_on(&d));

    // ── the watchdog: the whole reason this module exists ──────────────────────
    // The Gaposa panel latches an error if an input is held past 30 s, and a stuck dehumidifier call runs
    // the compressor indefinitely. An overrun must land OFF and LATCH, not quietly recover.
    ha_dout_cfg_t wd = { .active_high = true, .max_on_ms = 10000 };
    ha_dout_init(&d, &wd, 0);
    ha_dout_set(&d, true, 1000);
    ha_dout_tick(&d, 10000);
    check("pre-cap still ON",             ha_dout_on(&d));
    check("cap trips",                    ha_dout_tick(&d, 11000) == HA_DOUT_FAULTED);
    check("  forced OFF",                !ha_dout_on(&d));
    check("  latched",                    ha_dout_faulted(&d));
    check("set() refused while faulted",  ha_dout_set(&d, true, 12000) == HA_DOUT_FAULTED);
    check("pulse refused while faulted",  ha_dout_pulse(&d, 100, 12000) == HA_DOUT_FAULTED);
    check("  stays OFF",                 !ha_dout_on(&d));
    ha_dout_clear_fault(&d, 20000);
    check("cleared",                     !ha_dout_faulted(&d));
    check("usable again",                 ha_dout_set(&d, true, 20000) == HA_DOUT_OK);

    // ── deadline reporting (used to arm a hardware one-shot) ───────────────────
    uint32_t delay = 0;
    ha_dout_init(&d, &pulse, 0);
    check("no deadline when idle",       !ha_dout_next_deadline_ms(&d, 1000, &delay));
    ha_dout_pulse(&d, 600, 1000);
    check("deadline reported",            ha_dout_next_deadline_ms(&d, 1000, &delay));
    check("  = pulse width",              delay == 600);
    check("  shrinks as time passes",     ha_dout_next_deadline_ms(&d, 1400, &delay) && delay == 200);

    // With a cap shorter than the pulse the cap must win — it is the safety bound.
    ha_dout_cfg_t tight = { .active_high = true, .max_on_ms = 300 };
    ha_dout_init(&d, &tight, 0);
    ha_dout_pulse(&d, 300, 1000);
    check("cap bounds the deadline",      ha_dout_next_deadline_ms(&d, 1000, &delay) && delay == 300);

    // ── millisecond wraparound ────────────────────────────────────────────────
    // esp_timer ms wraps every ~49.7 days; an edge node runs far longer between reboots. Naive `>=`
    // comparisons would fire every deadline at once, or stall one forever, exactly once per wrap.
    const uint32_t near_wrap = 0xFFFFFF00u;   // 256 ms before the wrap
    ha_dout_init(&d, &pulse, near_wrap);
    check("pulse across wrap applies",    ha_dout_pulse(&d, 500, near_wrap) == HA_DOUT_OK);
    ha_dout_tick(&d, 0xF3u);                  // 499 ms later, having wrapped
    check("  still ON just before end",   ha_dout_on(&d));
    ha_dout_tick(&d, 0xF4u);                  // 500 ms later
    check("  OFF at end across wrap",    !ha_dout_on(&d));

    ha_dout_cfg_t wdwrap = { .active_high = true, .max_on_ms = 400 };
    ha_dout_init(&d, &wdwrap, near_wrap);
    ha_dout_set(&d, true, near_wrap);
    ha_dout_tick(&d, 0x90u);                  // 400 ms later, across the wrap
    check("watchdog trips across wrap",   ha_dout_faulted(&d));

    // init sits 256 ms before the wrap, so a post-wrap reading of X is (X + 256) ms after init.
    ha_dout_init(&d, &dwell, near_wrap);      // min_off 3000
    check("ON deferred across wrap",      ha_dout_set(&d, true, 0x40u) == HA_DOUT_DEFERRED);
    ha_dout_tick(&d, 0x900u);                 // 2560 ms after init — not yet
    check("  still OFF",                 !ha_dout_on(&d));
    ha_dout_tick(&d, 0xC00u);                 // 3328 ms after init
    check("  ON once dwell elapsed",      ha_dout_on(&d));

    printf("\n%s (%d failure%s)\n", fails ? "FAILED" : "ALL PASS", fails, fails == 1 ? "" : "s");
    return fails ? 1 : 0;
}
