// ha_gaposa — Gaposa QCTZ36SDU shade-panel command planner (ADR-0041).
// Pure: no ESP deps, no GPIO, host-tested. See include/ha_gaposa.h for the contract.
#include "ha_gaposa.h"

#include <string.h>

#define DEFAULT_PULSE_MS   500u
#define DEFAULT_INTERIM_MS 3000u
#define DEFAULT_GAP_MS     250u

static inline bool reached(uint32_t now, uint32_t deadline) {
    return (int32_t)(now - deadline) >= 0;   // wrap-safe; ms counters roll every ~49.7 days
}

static inline bool is_travel(ha_gaposa_cmd_t c) {
    return c == HA_GAPOSA_CMD_UP || c == HA_GAPOSA_CMD_DOWN;
}

// How long this command type holds its contact closed.
static uint32_t pulse_for(const ha_gaposa_t *g, ha_gaposa_cmd_t cmd) {
    return (cmd == HA_GAPOSA_CMD_INTERIM) ? g->cfg.interim_hold_ms : g->cfg.pulse_ms;
}

bool ha_gaposa_init(ha_gaposa_t *g, const ha_gaposa_cfg_t *cfg, uint32_t now_ms) {
    if (!g || !cfg) return false;
    if (cfg->channels == 0 || cfg->channels > HA_GAPOSA_MAX_CH) return false;

    memset(g, 0, sizeof(*g));
    g->cfg = *cfg;
    if (g->cfg.pulse_ms == 0)        g->cfg.pulse_ms = DEFAULT_PULSE_MS;
    if (g->cfg.interim_hold_ms == 0) g->cfg.interim_hold_ms = DEFAULT_INTERIM_MS;
    if (g->cfg.gap_ms == 0)          g->cfg.gap_ms = DEFAULT_GAP_MS;

    // Reject rather than clamp. Both of these are bounded well under the panel's 30 s lockout, and a
    // caller asking for more has misunderstood the hardware.
    if (g->cfg.pulse_ms > HA_GAPOSA_MAX_PULSE_MS) return false;
    if (g->cfg.interim_hold_ms > HA_GAPOSA_MAX_PULSE_MS) return false;

    for (uint8_t i = 0; i < HA_GAPOSA_MAX_CH; i++) {
        g->ch[i].conf = HA_GAPOSA_POS_UNKNOWN;   // nothing is known until a run reaches a limit
    }
    g->batch        = HA_GAPOSA_CMD_NONE;
    g->gap_until_ms = now_ms;
    g->inited       = true;
    return true;
}

bool ha_gaposa_command(ha_gaposa_t *g, uint8_t channel, ha_gaposa_cmd_t cmd, uint32_t now_ms) {
    (void)now_ms;
    if (!g || !g->inited) return false;
    if (channel >= g->cfg.channels) return false;
    if (cmd == HA_GAPOSA_CMD_NONE) return false;

    // Newest intent wins. Queuing UP behind DOWN would drive the shade somewhere nobody asked for.
    g->ch[channel].pending     = cmd;
    g->ch[channel].pending_seq = ++g->seq;
    return true;
}

// Dead-reckon a moving channel forward to `now`. Landing exactly on a limit is the only way a channel
// earns HA_GAPOSA_POS_EXACT — that is the motor's own hard stop re-datuming our estimate.
static void advance(ha_gaposa_t *g, uint8_t i, uint32_t now_ms) {
    ha_gaposa_ch_t *c = &g->ch[i];
    if (!c->moving) return;

    uint32_t span = c->moving_up ? g->cfg.travel_up_ms[i] : g->cfg.travel_down_ms[i];
    if (span == 0) {                 // no travel time configured: we cannot model this channel at all
        c->conf = HA_GAPOSA_POS_UNKNOWN;
        return;
    }

    uint32_t el = (uint32_t)(now_ms - c->move_start_ms);
    // Travel is in per-mille of a full run; 64-bit keeps a long travel time from overflowing the product.
    uint32_t delta = (uint32_t)(((uint64_t)el * HA_GAPOSA_OPEN) / span);

    if (c->moving_up) {
        uint32_t p = (uint32_t)c->move_start_pos + delta;
        if (p >= HA_GAPOSA_OPEN) {
            c->pos = HA_GAPOSA_OPEN;
            c->moving = false;
            c->conf = HA_GAPOSA_POS_EXACT;     // parked against the open limit
        } else {
            c->pos = (uint16_t)p;
            c->conf = HA_GAPOSA_POS_ESTIMATED;
        }
    } else {
        if (delta >= (uint32_t)c->move_start_pos) {
            c->pos = HA_GAPOSA_CLOSED;
            c->moving = false;
            c->conf = HA_GAPOSA_POS_EXACT;     // parked against the closed limit
        } else {
            c->pos = (uint16_t)(c->move_start_pos - delta);
            c->conf = HA_GAPOSA_POS_ESTIMATED;
        }
    }
}

// Apply the effect of a batch we just finished transmitting.
static void batch_effect(ha_gaposa_t *g, uint32_t now_ms) {
    for (uint8_t i = 0; i < g->cfg.channels; i++) {
        if (!(g->batch_mask & (1u << i))) continue;
        ha_gaposa_ch_t *c = &g->ch[i];
        c->last_sent = g->batch;

        switch (g->batch) {
        case HA_GAPOSA_CMD_UP:
        case HA_GAPOSA_CMD_DOWN:
            c->moving        = true;
            c->moving_up     = (g->batch == HA_GAPOSA_CMD_UP);
            c->move_start_ms = now_ms;
            c->move_start_pos = c->pos;
            if (c->conf == HA_GAPOSA_POS_EXACT) c->conf = HA_GAPOSA_POS_ESTIMATED;
            break;
        case HA_GAPOSA_CMD_STOP:
            // advance() has already marked a mid-travel channel ESTIMATED, which is exactly where the
            // estimate stops being trustworthy. A stop against a limit leaves EXACT intact — nothing moved.
            c->moving = false;
            break;
        case HA_GAPOSA_CMD_INTERIM:
            // The motor drives itself to its stored intermediate position. We do not know where that is
            // in per-mille terms, so the honest answer is that the position is no longer estimable.
            c->moving = false;
            c->conf   = HA_GAPOSA_POS_UNKNOWN;
            break;
        default:
            break;
        }
    }
}

// Choose the next batch: the command type of the OLDEST pending request, and every channel that wants
// that same type. This is what keeps the box-wide interlock satisfied — one type on the wire at a time.
static void start_batch(ha_gaposa_t *g, uint32_t now_ms) {
    ha_gaposa_cmd_t want = HA_GAPOSA_CMD_NONE;
    uint32_t oldest = 0;

    for (uint8_t i = 0; i < g->cfg.channels; i++) {
        if (g->ch[i].pending == HA_GAPOSA_CMD_NONE) continue;
        if (want == HA_GAPOSA_CMD_NONE || g->ch[i].pending_seq < oldest) {
            want   = g->ch[i].pending;
            oldest = g->ch[i].pending_seq;
        }
    }
    if (want == HA_GAPOSA_CMD_NONE) return;

    uint8_t mask = 0;
    for (uint8_t i = 0; i < g->cfg.channels; i++) {
        if (g->ch[i].pending == want) {
            mask |= (uint8_t)(1u << i);
            g->ch[i].pending = HA_GAPOSA_CMD_NONE;
        }
    }

    g->batch        = want;
    g->batch_mask   = mask;
    g->batch_end_ms = now_ms + pulse_for(g, want);
    g->in_gap       = false;
}

void ha_gaposa_tick(ha_gaposa_t *g, uint32_t now_ms, ha_gaposa_out_t *out) {
    if (out) memset(out, 0, sizeof(*out));
    if (!g || !g->inited) return;

    for (uint8_t i = 0; i < g->cfg.channels; i++) advance(g, i, now_ms);

    if (g->batch != HA_GAPOSA_CMD_NONE && reached(now_ms, g->batch_end_ms)) {
        batch_effect(g, now_ms);            // release: the pulse is over
        g->batch        = HA_GAPOSA_CMD_NONE;
        g->batch_mask   = 0;
        g->in_gap       = true;
        g->gap_until_ms = now_ms + g->cfg.gap_ms;
    }

    // The guard gap is what makes the serialisation real: two different command types must never be on
    // the wire in the same instant, and releasing one exactly as the next is asserted is too close.
    if (g->batch == HA_GAPOSA_CMD_NONE && (!g->in_gap || reached(now_ms, g->gap_until_ms))) {
        g->in_gap = false;
        start_batch(g, now_ms);
    }

    if (out && g->batch != HA_GAPOSA_CMD_NONE) {
        out->batch = g->batch;
        for (uint8_t i = 0; i < g->cfg.channels; i++) {
            if (g->batch_mask & (1u << i)) out->assert_ch[i] = g->batch;
        }
    }
}

uint16_t ha_gaposa_position(const ha_gaposa_t *g, uint8_t channel, ha_gaposa_conf_t *out_conf) {
    if (!g || !g->inited || channel >= g->cfg.channels) {
        if (out_conf) *out_conf = HA_GAPOSA_POS_UNKNOWN;
        return 0;
    }
    if (out_conf) *out_conf = g->ch[channel].conf;
    return g->ch[channel].pos;
}

ha_gaposa_cmd_t ha_gaposa_commanded(const ha_gaposa_t *g, uint8_t channel) {
    if (!g || !g->inited || channel >= g->cfg.channels) return HA_GAPOSA_CMD_NONE;
    return g->ch[channel].last_sent;
}

bool ha_gaposa_busy(const ha_gaposa_t *g) {
    if (!g || !g->inited) return false;
    if (g->batch != HA_GAPOSA_CMD_NONE) return true;
    for (uint8_t i = 0; i < g->cfg.channels; i++) {
        if (g->ch[i].pending != HA_GAPOSA_CMD_NONE) return true;
    }
    return false;
}

bool ha_gaposa_moving(const ha_gaposa_t *g, uint8_t channel) {
    if (!g || !g->inited || channel >= g->cfg.channels) return false;
    return g->ch[channel].moving;
}
