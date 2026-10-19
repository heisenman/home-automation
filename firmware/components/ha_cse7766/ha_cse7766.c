// CSE7766 frame parser — see include/ha_cse7766.h. Follows ESPHome's cse7766 component (the datasheet's
// worked rules: the 50 mA current floor, "power cycle over range" means 0 W, slow-cycle power hold).
#include "ha_cse7766.h"

#include <stddef.h>

#define MIN_HELD_POWER_CYCLE 1000000u   // µs: the chip only reports "time since last pulse" past 1 s cycles

static uint32_t u24(const uint8_t *p) { return ((uint32_t)p[0] << 16) | ((uint32_t)p[1] << 8) | p[2]; }

bool ha_cse_feed(ha_cse_framer_t *f, uint8_t b) {
    if (f->idx >= HA_CSE_FRAME) f->idx = 0;
    f->buf[f->idx] = b;
    bool good = true;
    if (f->idx == 0)
        good = (b == 0x55) || ((b & 0xF0) == 0xF0) || (b == 0xAA);
    else if (f->idx == 1)
        good = (b == 0x5A);
    else if (f->idx == HA_CSE_FRAME - 1) {
        uint8_t sum = 0;
        for (int i = 2; i < HA_CSE_FRAME - 1; i++) sum += f->buf[i];
        good = (sum == b);
    }
    if (!good) {
        // resync: a bad byte at [1] may itself be a header byte
        f->idx = 0;
        if (b == 0x55 || (b & 0xF0) == 0xF0 || b == 0xAA) { f->buf[0] = b; f->idx = 1; }
        return false;
    }
    if (f->idx == HA_CSE_FRAME - 1) { f->idx = 0; return true; }
    f->idx++;
    return false;
}

void ha_cse_parse(ha_cse_state_t *st, const uint8_t d[HA_CSE_FRAME], ha_cse_reading_t *out) {
    *out = (ha_cse_reading_t){ .status = d[0], .power_factor = -1.0f };
    uint8_t h = d[0];
    if (h == 0xAA) return;                                   // not calibrated
    bool p_over = false;
    if ((h & 0xF0) == 0xF0) {
        if (h & 0x0D) return;                                // V/I cycle over range or coefficients bad
        p_over = (h & 0x02) != 0;
    }
    out->ok = true;

    uint32_t vc = u24(d + 2), vy = u24(d + 5), ic = u24(d + 8), iy = u24(d + 11), pc = u24(d + 14),
             py = u24(d + 17);
    uint8_t adj = d[20];
    uint16_t cf = (uint16_t)((d[21] << 8) | d[22]);
    out->have_p = adj & 0x10;
    out->have_i = adj & 0x20;
    out->have_v = adj & 0x40;

    if (out->have_v && vy) out->voltage_v = (float)vc / (float)vy;
    else out->have_v = false;

    // energy: the 16-bit pulse counter wraps; unsigned subtraction handles one wrap per frame (~50 ms)
    if (!st->cf_seen) { st->cf_last = cf; st->cf_seen = true; }
    st->cf_total += (uint16_t)(cf - st->cf_last);
    st->cf_last = cf;
    st->power_coeff = pc;

    if (p_over) {
        out->have_p = true;
        st->last_power_cycle = 0;
        out->power_w = 0.0f;
    } else if (out->have_p && py) {
        out->power_w = (float)pc / (float)py;
        st->last_power_cycle = py;
    } else if (st->last_power_cycle > MIN_HELD_POWER_CYCLE && py <= 2u * st->last_power_cycle) {
        out->have_p = true;                                  // slow light-load cycle: next pulse not yet due
        out->power_w = (float)pc / (float)st->last_power_cycle;
    } else {
        out->have_p = true;                                  // pulse overdue: the load dropped
        st->last_power_cycle = 0;
        out->power_w = 0.0f;
    }

    if (out->have_i) {
        float calc = (out->have_p && out->voltage_v > 1.0f) ? out->power_w / out->voltage_v : 0.0f;
        out->current_a = (calc > 0.05f && iy) ? (float)ic / (float)iy : 0.0f;   // datasheet floor: 50 mA
    }

    if (out->have_v && out->have_i) {
        out->apparent_va = out->voltage_v * out->current_a;
        if (out->apparent_va > 0.0f) {
            float pf = out->power_w / out->apparent_va;
            out->power_factor = pf > 1.0f ? 1.0f : (pf < 0.0f ? 0.0f : pf);
        } else if (out->power_w == 0.0f) {
            out->power_factor = 1.0f;                        // no load
        }
    }
}

double ha_cse_energy_wh(const ha_cse_state_t *st) {
    return (double)st->cf_total * (double)st->power_coeff / 1e6 / 3600.0;
}
