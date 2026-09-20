// Host unit test for ha_gaposa (no ESP deps). Run via ./run.sh, or manually:
//   cc test/test_ha_gaposa.c ha_gaposa.c -Iinclude -o /tmp/t && /tmp/t
#include "ha_gaposa.h"
#include <stdio.h>
#include <string.h>

static int fails = 0;
static void check(const char *name, int cond) {
    printf("%s  %s\n", cond ? "PASS" : "FAIL", name);
    if (!cond) fails++;
}

// THE invariant. The panel latches an error — all LEDs on, transmission stops — if two channels are
// driven with different commands at the same instant. Every tick in every scenario must satisfy this.
static int violations = 0;
static void assert_invariant(const ha_gaposa_out_t *o, uint8_t nch) {
    ha_gaposa_cmd_t seen = HA_GAPOSA_CMD_NONE;
    for (uint8_t i = 0; i < nch; i++) {
        ha_gaposa_cmd_t c = o->assert_ch[i];
        if (c == HA_GAPOSA_CMD_NONE) continue;
        if (c != o->batch) { violations++; return; }        // per-channel must match the batch
        if (seen != HA_GAPOSA_CMD_NONE && c != seen) { violations++; return; }
        seen = c;
    }
}

// Run the planner from t0 to t1 in 10 ms steps, checking the invariant at every step.
// Returns a mask of channels that were asserted at least once, and records the batch order.
static uint8_t run(ha_gaposa_t *g, uint32_t t0, uint32_t t1, uint8_t nch,
                   ha_gaposa_cmd_t *order, int *norder, uint8_t *order_mask) {
    ha_gaposa_out_t o;
    uint8_t touched = 0;
    ha_gaposa_cmd_t prev = HA_GAPOSA_CMD_NONE;
    for (uint32_t t = t0; t <= t1; t += 10) {
        ha_gaposa_tick(g, t, &o);
        assert_invariant(&o, nch);
        if (o.batch != HA_GAPOSA_CMD_NONE && o.batch != prev && norder && *norder < 16) {
            order[*norder] = o.batch;
            uint8_t m = 0;
            for (uint8_t i = 0; i < nch; i++) if (o.assert_ch[i] != HA_GAPOSA_CMD_NONE) m |= (uint8_t)(1u << i);
            if (order_mask) order_mask[*norder] = m;
            (*norder)++;
        }
        prev = o.batch;
        for (uint8_t i = 0; i < nch; i++) if (o.assert_ch[i] != HA_GAPOSA_CMD_NONE) touched |= (uint8_t)(1u << i);
    }
    return touched;
}

int main(void) {
    ha_gaposa_t g;
    ha_gaposa_out_t o;

    // ── config validation ─────────────────────────────────────────────────────
    ha_gaposa_cfg_t bad = { .channels = 0 };
    check("zero channels rejected",  !ha_gaposa_init(&g, &bad, 0));
    bad.channels = HA_GAPOSA_MAX_CH + 1;
    check("too many channels rejected", !ha_gaposa_init(&g, &bad, 0));
    ha_gaposa_cfg_t longpulse = { .channels = 1, .pulse_ms = HA_GAPOSA_MAX_PULSE_MS + 1 };
    check("over-long pulse rejected", !ha_gaposa_init(&g, &longpulse, 0));
    ha_gaposa_cfg_t longhold = { .channels = 1, .interim_hold_ms = HA_GAPOSA_MAX_PULSE_MS + 1 };
    check("over-long interim rejected", !ha_gaposa_init(&g, &longhold, 0));

    ha_gaposa_cfg_t cfg = {
        .channels = 3, .pulse_ms = 500, .gap_ms = 250, .interim_hold_ms = 3000,
        .travel_up_ms   = { 20000, 20000, 20000 },
        .travel_down_ms = { 18000, 18000, 18000 },
    };
    check("valid config accepted",    ha_gaposa_init(&g, &cfg, 0));
    check("bad channel refused",     !ha_gaposa_command(&g, 3, HA_GAPOSA_CMD_UP, 0));
    check("NONE refused",            !ha_gaposa_command(&g, 0, HA_GAPOSA_CMD_NONE, 0));
    check("idle is not busy",        !ha_gaposa_busy(&g));

    // ── a single command ──────────────────────────────────────────────────────
    ha_gaposa_init(&g, &cfg, 0);
    ha_gaposa_command(&g, 0, HA_GAPOSA_CMD_UP, 0);
    check("queued -> busy",           ha_gaposa_busy(&g));
    ha_gaposa_tick(&g, 0, &o);
    check("asserts UP on ch0",        o.assert_ch[0] == HA_GAPOSA_CMD_UP && o.batch == HA_GAPOSA_CMD_UP);
    check("  ch1 untouched",          o.assert_ch[1] == HA_GAPOSA_CMD_NONE);
    ha_gaposa_tick(&g, 490, &o);
    check("still asserted at 490",    o.assert_ch[0] == HA_GAPOSA_CMD_UP);
    ha_gaposa_tick(&g, 500, &o);
    check("released at pulse end",    o.assert_ch[0] == HA_GAPOSA_CMD_NONE && o.batch == HA_GAPOSA_CMD_NONE);
    check("commanded state recorded", ha_gaposa_commanded(&g, 0) == HA_GAPOSA_CMD_UP);

    // ── ⛔ the box-wide interlock: mixed directions must serialise ────────────
    violations = 0;
    ha_gaposa_init(&g, &cfg, 0);
    ha_gaposa_command(&g, 0, HA_GAPOSA_CMD_DOWN, 0);
    ha_gaposa_command(&g, 1, HA_GAPOSA_CMD_DOWN, 0);
    ha_gaposa_command(&g, 2, HA_GAPOSA_CMD_UP,   0);   // the "close all except the office" shape

    ha_gaposa_cmd_t order[16]; int norder = 0; uint8_t omask[16];
    memset(order, 0, sizeof order); memset(omask, 0, sizeof omask);
    uint8_t touched = run(&g, 0, 4000, 3, order, &norder, omask);

    check("no interlock violation",   violations == 0);
    check("all three channels sent",  touched == 0x07);
    check("exactly two batches",      norder == 2);
    check("  DOWN first (oldest)",    norder >= 1 && order[0] == HA_GAPOSA_CMD_DOWN);
    check("  ch0+ch1 batched together", norder >= 1 && omask[0] == 0x03);
    check("  UP second",              norder >= 2 && order[1] == HA_GAPOSA_CMD_UP);
    check("  ch2 alone",              norder >= 2 && omask[1] == 0x04);
    check("drains to idle",          !ha_gaposa_busy(&g));

    // Same command across channels is legal and must NOT be split — it is the efficient "all up" path.
    violations = 0;
    ha_gaposa_init(&g, &cfg, 0);
    for (uint8_t i = 0; i < 3; i++) ha_gaposa_command(&g, i, HA_GAPOSA_CMD_UP, 0);
    norder = 0; memset(omask, 0, sizeof omask);
    run(&g, 0, 2000, 3, order, &norder, omask);
    check("same command = one batch",  norder == 1);
    check("  all three together",      omask[0] == 0x07);
    check("  no violation",            violations == 0);

    // All five command types queued at once still never overlap.
    violations = 0;
    ha_gaposa_init(&g, &cfg, 0);
    ha_gaposa_command(&g, 0, HA_GAPOSA_CMD_UP,      0);
    ha_gaposa_command(&g, 1, HA_GAPOSA_CMD_STOP,    0);
    ha_gaposa_command(&g, 2, HA_GAPOSA_CMD_INTERIM, 0);
    norder = 0;
    touched = run(&g, 0, 12000, 3, order, &norder, omask);
    check("three types serialise",     norder == 3);
    check("  all delivered",           touched == 0x07);
    check("  no violation",            violations == 0);

    // There must be real quiet time between batches — releasing one as the next asserts is too close.
    ha_gaposa_init(&g, &cfg, 0);
    ha_gaposa_command(&g, 0, HA_GAPOSA_CMD_UP,   0);
    ha_gaposa_command(&g, 1, HA_GAPOSA_CMD_DOWN, 0);
    ha_gaposa_tick(&g, 0, &o);
    check("gap: UP asserted first",     o.batch == HA_GAPOSA_CMD_UP);
    ha_gaposa_tick(&g, 500, &o);
    check("gap: nothing at release",   o.batch == HA_GAPOSA_CMD_NONE);
    ha_gaposa_tick(&g, 740, &o);
    check("gap: still nothing at 740", o.batch == HA_GAPOSA_CMD_NONE);
    ha_gaposa_tick(&g, 750, &o);
    check("gap: next batch at 750",    o.batch == HA_GAPOSA_CMD_DOWN);

    // ── newest intent wins ────────────────────────────────────────────────────
    ha_gaposa_init(&g, &cfg, 0);
    ha_gaposa_command(&g, 0, HA_GAPOSA_CMD_UP,   0);
    ha_gaposa_command(&g, 0, HA_GAPOSA_CMD_DOWN, 10);   // changed their mind before anything was sent
    norder = 0;
    run(&g, 0, 2000, 3, order, &norder, omask);
    check("superseded command dropped", norder == 1);
    check("  only DOWN sent",           order[0] == HA_GAPOSA_CMD_DOWN);
    check("  commanded = DOWN",         ha_gaposa_commanded(&g, 0) == HA_GAPOSA_CMD_DOWN);

    // ── position model + confidence ───────────────────────────────────────────
    ha_gaposa_conf_t conf;
    ha_gaposa_init(&g, &cfg, 0);
    ha_gaposa_position(&g, 0, &conf);
    check("starts UNKNOWN",             conf == HA_GAPOSA_POS_UNKNOWN);

    ha_gaposa_command(&g, 0, HA_GAPOSA_CMD_UP, 0);
    ha_gaposa_tick(&g, 0, &o);
    ha_gaposa_tick(&g, 500, &o);                       // pulse ends; motion starts
    check("moving after the pulse",     ha_gaposa_moving(&g, 0));
    ha_gaposa_tick(&g, 10500, &o);                     // half of a 20 s run
    uint16_t pos = ha_gaposa_position(&g, 0, &conf);
    check("half travel ~500",           pos > 480 && pos < 520);
    check("  and ESTIMATED",            conf == HA_GAPOSA_POS_ESTIMATED);

    ha_gaposa_tick(&g, 20500, &o);                     // full run
    pos = ha_gaposa_position(&g, 0, &conf);
    check("full travel = OPEN",         pos == HA_GAPOSA_OPEN);
    check("  and EXACT at the limit",   conf == HA_GAPOSA_POS_EXACT);
    check("  no longer moving",        !ha_gaposa_moving(&g, 0));

    // A stop mid-travel is precisely where the estimate stops being trustworthy.
    ha_gaposa_init(&g, &cfg, 0);
    ha_gaposa_command(&g, 0, HA_GAPOSA_CMD_UP, 0);
    ha_gaposa_tick(&g, 0, &o); ha_gaposa_tick(&g, 500, &o);
    ha_gaposa_tick(&g, 20500, &o);                     // drive fully open -> EXACT
    ha_gaposa_position(&g, 0, &conf);
    check("open is EXACT",              conf == HA_GAPOSA_POS_EXACT);
    ha_gaposa_command(&g, 0, HA_GAPOSA_CMD_DOWN, 20500);
    ha_gaposa_tick(&g, 20500, &o);
    ha_gaposa_position(&g, 0, &conf);
    check("still EXACT while sending",  conf == HA_GAPOSA_POS_EXACT);  // motor has not received it yet
    ha_gaposa_tick(&g, 21000, &o);                                     // pulse ends -> motion begins
    ha_gaposa_position(&g, 0, &conf);
    check("moving demotes EXACT",       conf != HA_GAPOSA_POS_EXACT);
    ha_gaposa_tick(&g, 30000, &o);
    ha_gaposa_command(&g, 0, HA_GAPOSA_CMD_STOP, 30000);
    run(&g, 30000, 32000, 3, NULL, NULL, NULL);
    pos = ha_gaposa_position(&g, 0, &conf);
    check("stopped mid-travel",        !ha_gaposa_moving(&g, 0));
    check("  stays ESTIMATED",          conf == HA_GAPOSA_POS_ESTIMATED);
    check("  position is between",      pos > 0 && pos < HA_GAPOSA_OPEN);
    check("commanded != estimated",     ha_gaposa_commanded(&g, 0) == HA_GAPOSA_CMD_STOP);

    // Interim recall: the motor goes somewhere we cannot model, and we say so rather than guess.
    ha_gaposa_init(&g, &cfg, 0);
    ha_gaposa_command(&g, 0, HA_GAPOSA_CMD_UP, 0);
    ha_gaposa_tick(&g, 0, &o); ha_gaposa_tick(&g, 500, &o); ha_gaposa_tick(&g, 20500, &o);
    ha_gaposa_position(&g, 0, &conf);
    check("open before interim",        conf == HA_GAPOSA_POS_EXACT);
    ha_gaposa_command(&g, 0, HA_GAPOSA_CMD_INTERIM, 20500);
    ha_gaposa_tick(&g, 20500, &o);
    check("interim holds St",           o.batch == HA_GAPOSA_CMD_INTERIM);
    ha_gaposa_tick(&g, 23000, &o);
    check("  still held at 2.5 s",      o.batch == HA_GAPOSA_CMD_INTERIM);
    ha_gaposa_tick(&g, 23500, &o);
    check("  released at 3 s",          o.batch == HA_GAPOSA_CMD_NONE);
    ha_gaposa_position(&g, 0, &conf);
    check("interim -> UNKNOWN",         conf == HA_GAPOSA_POS_UNKNOWN);

    // A channel with no configured travel time must not pretend to know anything.
    ha_gaposa_cfg_t nomodel = { .channels = 1, .pulse_ms = 500, .gap_ms = 250 };
    ha_gaposa_init(&g, &nomodel, 0);
    ha_gaposa_command(&g, 0, HA_GAPOSA_CMD_UP, 0);
    run(&g, 0, 30000, 1, NULL, NULL, NULL);
    ha_gaposa_position(&g, 0, &conf);
    check("no travel time -> UNKNOWN",  conf == HA_GAPOSA_POS_UNKNOWN);

    // ── wraparound ────────────────────────────────────────────────────────────
    const uint32_t near_wrap = 0xFFFFFF00u;   // 256 ms before the wrap
    violations = 0;
    ha_gaposa_init(&g, &cfg, near_wrap);
    ha_gaposa_command(&g, 0, HA_GAPOSA_CMD_UP,   near_wrap);
    ha_gaposa_command(&g, 1, HA_GAPOSA_CMD_DOWN, near_wrap);
    ha_gaposa_tick(&g, near_wrap, &o);
    check("asserts before the wrap",    o.batch == HA_GAPOSA_CMD_UP);
    ha_gaposa_tick(&g, 0xF4u, &o);            // 500 ms later, having wrapped
    check("releases across the wrap",   o.batch == HA_GAPOSA_CMD_NONE);
    ha_gaposa_tick(&g, 0x1EEu, &o);           // +250 ms gap
    check("next batch across the wrap", o.batch == HA_GAPOSA_CMD_DOWN);
    check("  no violation",             violations == 0);

    printf("\n%s (%d failure%s, %d interlock violation%s)\n",
           (fails || violations) ? "FAILED" : "ALL PASS",
           fails, fails == 1 ? "" : "s", violations, violations == 1 ? "" : "s");
    return (fails || violations) ? 1 : 0;
}
