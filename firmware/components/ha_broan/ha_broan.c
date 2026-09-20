// ha_broan — ERV session state machine (ADR-0041). Pure: bytes in, bytes out, no transport, no ESP deps.
// That is what lets the whole token conversation be proven against a simulated ERV on the host.
// See include/ha_broan.h for the contract.
#include "ha_broan.h"

#include <string.h>

#define DEFAULT_POLL_MS     5000u
#define DEFAULT_REPLY_MS    1000u

// Default telemetry sweep. Deliberately more than BROAN_MAX_READ_REGS (10) so the chunking path is the
// normal path rather than an untrodden edge case.
static const uint16_t kDefaultPoll[] = {
    BROAN_REG_FAN_MODE,   BROAN_REG_ACTIVE_MODE, BROAN_REG_POWER_W,
    BROAN_REG_TEMP_SUPPLY, BROAN_REG_TEMP_EXHAUST,
    BROAN_REG_CFM_SUPPLY, BROAN_REG_CFM_EXHAUST,
    BROAN_REG_RPM_SUPPLY, BROAN_REG_RPM_EXHAUST,
    BROAN_REG_FILTER_LIFE, BROAN_REG_FAULT, BROAN_REG_WARNING, BROAN_REG_UPTIME,
};

static inline bool elapsed(uint32_t now, uint32_t since, uint32_t interval) {
    return (uint32_t)(now - since) >= interval;   // wrap-safe
}

static const uint16_t *poll_list(const ha_broan_t *b, uint8_t *count) {
    if (b->cfg.poll_regs && b->cfg.poll_reg_count) {
        *count = b->cfg.poll_reg_count;
        return b->cfg.poll_regs;
    }
    *count = (uint8_t)(sizeof(kDefaultPoll) / sizeof(kDefaultPoll[0]));
    return kDefaultPoll;
}

void ha_broan_init(ha_broan_t *b, const ha_broan_cfg_t *cfg, uint32_t now_ms) {
    if (!b) return;
    memset(b, 0, sizeof(*b));
    if (cfg) b->cfg = *cfg;

    if (b->cfg.our_addr == 0)           b->cfg.our_addr = BROAN_ADDR_CLIENT;
    if (b->cfg.erv_addr == 0)           b->cfg.erv_addr = BROAN_ADDR_ERV;
    if (b->cfg.heartbeat_ms == 0)       b->cfg.heartbeat_ms = BROAN_HEARTBEAT_MS;
    if (b->cfg.control_timeout_ms == 0) b->cfg.control_timeout_ms = BROAN_CONTROL_TIMEOUT_MS;
    if (b->cfg.poll_ms == 0)            b->cfg.poll_ms = DEFAULT_POLL_MS;
    if (b->cfg.reply_timeout_ms == 0)   b->cfg.reply_timeout_ms = DEFAULT_REPLY_MS;

    b->last_token_ms     = now_ms;
    b->last_heartbeat_ms = now_ms;
    b->last_poll_ms      = now_ms;
    b->poll_sweeping     = true;    // sweep once at startup so the cache fills promptly
    b->inited            = true;
}

// ── field cache ───────────────────────────────────────────────────────────────

static ha_broan_field_t *field_find(ha_broan_t *b, uint16_t reg) {
    for (uint8_t i = 0; i < b->nfields; i++) {
        if (b->fields[i].reg == reg) return &b->fields[i];
    }
    return NULL;
}

static void field_store(ha_broan_t *b, const broan_tlv_t *t, uint32_t now_ms) {
    ha_broan_field_t *f = field_find(b, t->reg);
    if (!f) {
        if (b->nfields >= HA_BROAN_MAX_FIELDS) return;   // cache full: drop, do not evict
        f = &b->fields[b->nfields++];
        f->reg = t->reg;
    }
    f->len = (t->len > 4) ? 4 : t->len;
    memset(f->raw, 0, sizeof(f->raw));
    if (t->data && f->len) memcpy(f->raw, t->data, f->len);
    f->updated_ms = now_ms;
    f->valid = true;
}

static const ha_broan_field_t *field_get(const ha_broan_t *b, uint16_t reg) {
    for (uint8_t i = 0; i < b->nfields; i++) {
        if (b->fields[i].reg == reg && b->fields[i].valid) return &b->fields[i];
    }
    return NULL;
}

// ── write queue ───────────────────────────────────────────────────────────────

static bool txq_push(ha_broan_t *b, const uint8_t *payload, size_t len) {
    if (len == 0 || len > HA_BROAN_MSG_MAX) return false;
    if (b->txq_count >= HA_BROAN_TXQ_DEPTH) {
        b->stats.writes_dropped++;
        return false;
    }
    uint8_t slot = (uint8_t)((b->txq_head + b->txq_count) % HA_BROAN_TXQ_DEPTH);
    memcpy(b->txq[slot].buf, payload, len);
    b->txq[slot].len = (uint8_t)len;
    b->txq_count++;
    return true;
}

static bool txq_pop(ha_broan_t *b, uint8_t *out, size_t *len) {
    if (b->txq_count == 0) return false;
    *len = b->txq[b->txq_head].len;
    memcpy(out, b->txq[b->txq_head].buf, *len);
    b->txq_head = (uint8_t)((b->txq_head + 1) % HA_BROAN_TXQ_DEPTH);
    b->txq_count--;
    return true;
}

// ── receive ───────────────────────────────────────────────────────────────────

static void handle_frame(ha_broan_t *b, const broan_frame_t *f, uint32_t now_ms) {
    b->stats.frames_rx++;
    b->seen_erv      = true;
    b->last_frame_ms = now_ms;

    if (f->len == 0) return;
    uint8_t op = f->payload[0];

    // Register responses are harvested regardless of who they were addressed to. A listen-only build
    // parked beside the real wall control therefore collects genuine telemetry — the cheapest possible
    // validation of the register map, before we ever take the bus.
    if (op == BROAN_MSG_READ_RESP) {
        size_t cur = 1;
        broan_tlv_t t;
        while (broan_tlv_next(f->payload, f->len, &cur, &t)) field_store(b, &t, now_ms);
    }

    // Everything below is session state and only applies to frames aimed at us.
    if (f->target != b->cfg.our_addr) return;

    switch (op) {
    case BROAN_MSG_PING:
        b->stats.pings++;
        b->pong_len = (f->len > BROAN_MAX_PAYLOAD) ? BROAN_MAX_PAYLOAD : f->len;
        memcpy(b->pong, f->payload, b->pong_len);
        b->want_pong = true;
        break;

    case BROAN_MSG_TOKEN_OFFER:
        // The ERV is handing us the bus. Ack, then send at most one message at a time until we are done
        // and give it back. It does not re-ping if we vanish, so a token offer is also our liveness sign.
        b->stats.tokens++;
        b->last_token_ms  = now_ms;
        b->control_timeout_latched = false;
        b->have_token     = true;
        b->awaiting_reply = false;
        b->want_token_ack = true;
        break;

    case BROAN_MSG_TOKEN_ACK:
        break;   // the ERV confirming it took the bus back

    case BROAN_MSG_READ_RESP:
        b->awaiting_reply = false;   // payload already harvested above
        break;

    case BROAN_MSG_WRITE_ACK:
        // The written registers are now stale in our cache; drop them so the next sweep re-reads rather
        // than reporting the pre-write value as current.
        for (size_t i = 1; i + 1 < f->len; i += 2) {
            uint16_t reg = (uint16_t)((uint16_t)f->payload[i] << 8 | f->payload[i + 1]);
            ha_broan_field_t *fl = field_find(b, reg);
            if (fl) fl->valid = false;
        }
        b->awaiting_reply = false;
        break;

    default:
        break;
    }
}

void ha_broan_rx(ha_broan_t *b, const uint8_t *data, size_t len, uint32_t now_ms) {
    if (!b || !b->inited || (!data && len)) return;

    for (size_t i = 0; i < len; i++) {
        if (b->rxlen >= sizeof(b->rxbuf)) {
            // Overflow means we are not finding frames in what is arriving. Keep the newest half rather
            // than dropping everything: the frame we want is far more likely to be at the tail.
            size_t keep = sizeof(b->rxbuf) / 2;
            memmove(b->rxbuf, b->rxbuf + (sizeof(b->rxbuf) - keep), keep);
            b->rxlen = keep;
        }
        b->rxbuf[b->rxlen++] = data[i];
    }

    for (;;) {
        broan_frame_t f;
        size_t consumed = 0;
        broan_decode_rc_t rc = broan_decode(b->rxbuf, b->rxlen, &f, &consumed);

        if (rc == BROAN_DECODE_NEED_MORE) break;
        if (rc == BROAN_DECODE_OK) {
            handle_frame(b, &f, now_ms);
        } else if (rc == BROAN_DECODE_BAD_CHECKSUM || rc == BROAN_DECODE_BAD_FRAMING) {
            b->stats.frames_bad++;
        }
        if (consumed == 0) break;                    // no progress possible
        memmove(b->rxbuf, b->rxbuf + consumed, b->rxlen - consumed);
        b->rxlen -= consumed;
    }
}

// ── transmit ──────────────────────────────────────────────────────────────────

static size_t emit(ha_broan_t *b, uint8_t *out, size_t cap,
                   const uint8_t *payload, size_t plen) {
    int n = broan_encode(out, cap, b->cfg.erv_addr, b->cfg.our_addr, payload, (uint8_t)plen);
    return (n > 0) ? (size_t)n : 0;
}

size_t ha_broan_next_tx(ha_broan_t *b, uint8_t *out, size_t cap, uint32_t now_ms) {
    if (!b || !b->inited || !out || cap == 0) return 0;

    // Defence in depth: ha_rs485 also refuses, but a bus where a stray write reaches a ventilator earns
    // two independent gates.
    if (b->cfg.listen_only) return 0;

    // The ERV has stopped yielding the token. The reference has no recovery for this beyond noticing;
    // neither do we, but we count it and go offline so a caller can react rather than silently believing
    // stale values.
    if (b->seen_erv && elapsed(now_ms, b->last_token_ms, b->cfg.control_timeout_ms)) {
        // Latch rather than re-arming last_token_ms: re-arming would make the stat fire once, but it
        // would also make ha_broan_online() report healthy again immediately, which is precisely the
        // condition a caller needs to see. The latch clears when a real token offer arrives.
        if (!b->control_timeout_latched) {
            b->stats.control_timeouts++;
            b->control_timeout_latched = true;
        }
        b->have_token     = false;
        b->awaiting_reply = false;
    }

    // A reply that never came would otherwise idle the whole token hold. The next token offer clears this
    // anyway, but giving up sooner means we still get useful work done inside this hold.
    if (b->awaiting_reply && elapsed(now_ms, b->awaiting_since_ms, b->cfg.reply_timeout_ms)) {
        b->stats.reply_timeouts++;
        b->awaiting_reply = false;
    }

    // A ping is answered immediately, token or not — it is how the ERV decides we exist.
    if (b->want_pong) {
        uint8_t pay[BROAN_MAX_PAYLOAD];
        int n = broan_build_pong(pay, sizeof pay, b->pong, b->pong_len);
        b->want_pong = false;
        if (n > 0) return emit(b, out, cap, pay, (size_t)n);
        return 0;
    }

    if (b->want_token_ack) {
        uint8_t pay[1];
        int n = broan_build_token_ack(pay, sizeof pay);
        b->want_token_ack = false;
        if (n > 0) return emit(b, out, cap, pay, (size_t)n);
        return 0;
    }

    if (!b->have_token || b->awaiting_reply) return 0;

    uint8_t pay[BROAN_MAX_PAYLOAD];

    // ⛔ HEARTBEAT FIRST, ahead of user writes and polling. Going quiet past the control timeout raises
    // E50 and shuts the unit down; a backlog of writes must never be the reason that happens.
    // The FIRST heartbeat goes out as soon as we hold the bus, not heartbeat_ms later. We have just
    // taken the wall control's slot, the control timeout is shorter than the heartbeat interval, and
    // waiting a full interval to first assert liveness is a gratuitous window on an E50.
    if (!b->heartbeat_primed || elapsed(now_ms, b->last_heartbeat_ms, b->cfg.heartbeat_ms)) {
        int n = broan_build_heartbeat(pay, sizeof pay);
        if (n > 0) {
            b->heartbeat_primed   = true;
            b->last_heartbeat_ms  = now_ms;
            b->awaiting_reply     = true;
            b->awaiting_since_ms  = now_ms;
            b->stats.heartbeats++;
            return emit(b, out, cap, pay, (size_t)n);
        }
    }

    size_t qlen = 0;
    if (txq_pop(b, pay, &qlen)) {
        b->awaiting_reply    = true;
        b->awaiting_since_ms = now_ms;
        b->stats.writes_sent++;
        return emit(b, out, cap, pay, qlen);
    }

    uint8_t npoll = 0;
    const uint16_t *regs = poll_list(b, &npoll);
    if (!b->poll_sweeping && elapsed(now_ms, b->last_poll_ms, b->cfg.poll_ms)) {
        b->poll_sweeping = true;
        b->poll_cursor   = 0;
    }
    if (b->poll_sweeping && npoll > 0) {
        uint8_t remaining = (uint8_t)(npoll - b->poll_cursor);
        uint8_t chunk = (remaining > BROAN_MAX_READ_REGS) ? (uint8_t)BROAN_MAX_READ_REGS : remaining;
        int n = broan_build_read(pay, sizeof pay, &regs[b->poll_cursor], chunk);
        if (n > 0) {
            b->poll_cursor = (uint8_t)(b->poll_cursor + chunk);
            if (b->poll_cursor >= npoll) {     // sweep complete
                b->poll_cursor   = 0;
                b->poll_sweeping = false;
                b->last_poll_ms  = now_ms;
            }
            b->awaiting_reply    = true;
            b->awaiting_since_ms = now_ms;
            b->stats.reads_sent++;
            return emit(b, out, cap, pay, (size_t)n);
        }
    }

    // Nothing left to do — hand the bus back rather than sitting on it.
    {
        uint8_t rel[1] = { BROAN_MSG_TOKEN_OFFER };
        b->have_token = false;
        return emit(b, out, cap, rel, 1);
    }
}

// ── control ───────────────────────────────────────────────────────────────────

bool ha_broan_set_fan_mode(ha_broan_t *b, uint8_t mode) {
    if (!b || !b->inited) return false;
    // OVR wedges the unit until cleared. Refusing it here as well as in the codec means no caller path
    // reaches it, however the value was computed.
    if (!broan_fan_mode_writable(mode)) return false;
    uint8_t pay[HA_BROAN_MSG_MAX];
    int n = broan_build_write_u8(pay, sizeof pay, BROAN_REG_FAN_MODE, mode);
    return n > 0 && txq_push(b, pay, (size_t)n);
}

bool ha_broan_set_manual_cfm(ha_broan_t *b, float supply, float exhaust) {
    if (!b || !b->inited) return false;
    uint8_t pay[HA_BROAN_MSG_MAX];
    int n = broan_build_write_f32(pay, sizeof pay, BROAN_REG_TARGET_CFM_IN, supply);
    if (n <= 0 || !txq_push(b, pay, (size_t)n)) return false;
    n = broan_build_write_f32(pay, sizeof pay, BROAN_REG_TARGET_CFM_OUT, exhaust);
    return n > 0 && txq_push(b, pay, (size_t)n);
}

bool ha_broan_report_humidity(ha_broan_t *b, float rh) {
    if (!b || !b->inited) return false;
    uint8_t pay[HA_BROAN_MSG_MAX];
    int n = broan_build_write_f32(pay, sizeof pay, BROAN_REG_CTRL_HUMIDITY, rh);
    return n > 0 && txq_push(b, pay, (size_t)n);
}

bool ha_broan_report_temperature(ha_broan_t *b, float temp) {
    if (!b || !b->inited) return false;
    uint8_t pay[HA_BROAN_MSG_MAX];
    int n = broan_build_write_f32(pay, sizeof pay, BROAN_REG_CTRL_TEMP, temp);
    return n > 0 && txq_push(b, pay, (size_t)n);
}

// ── cached reads ──────────────────────────────────────────────────────────────

static bool as_tlv(const ha_broan_field_t *f, broan_tlv_t *t) {
    if (!f) return false;
    t->reg  = f->reg;
    t->len  = f->len;
    t->data = f->raw;
    return true;
}

bool ha_broan_get_f32(const ha_broan_t *b, uint16_t reg, float *out) {
    if (!b || !out) return false;
    broan_tlv_t t;
    const ha_broan_field_t *f = field_get(b, reg);
    if (!as_tlv(f, &t) || t.len < 4) return false;
    *out = broan_tlv_f32(&t);
    return true;
}

bool ha_broan_get_i32(const ha_broan_t *b, uint16_t reg, int32_t *out) {
    if (!b || !out) return false;
    broan_tlv_t t;
    const ha_broan_field_t *f = field_get(b, reg);
    if (!as_tlv(f, &t) || t.len < 4) return false;
    *out = broan_tlv_i32(&t);
    return true;
}

bool ha_broan_get_u8(const ha_broan_t *b, uint16_t reg, uint8_t *out) {
    if (!b || !out) return false;
    broan_tlv_t t;
    const ha_broan_field_t *f = field_get(b, reg);
    if (!as_tlv(f, &t) || t.len < 1) return false;
    *out = broan_tlv_u8(&t);
    return true;
}

uint32_t ha_broan_age_ms(const ha_broan_t *b, uint16_t reg, uint32_t now_ms) {
    const ha_broan_field_t *f = b ? field_get(b, reg) : NULL;
    if (!f) return UINT32_MAX;
    return (uint32_t)(now_ms - f->updated_ms);
}

// ── health ────────────────────────────────────────────────────────────────────

bool ha_broan_have_token(const ha_broan_t *b) {
    return b && b->inited && b->have_token;
}

bool ha_broan_online(const ha_broan_t *b, uint32_t now_ms) {
    if (!b || !b->inited || !b->seen_erv) return false;
    return !elapsed(now_ms, b->last_token_ms, b->cfg.control_timeout_ms);
}

uint32_t ha_broan_e50_exposure_ms(const ha_broan_t *b, uint32_t now_ms) {
    if (!b || !b->inited) return 0;
    if (b->stats.heartbeats == 0) return 0;   // we have not taken the bus, so nothing is owed yet
    return (uint32_t)(now_ms - b->last_heartbeat_ms);
}

void ha_broan_get_stats(const ha_broan_t *b, ha_broan_stats_t *out) {
    if (!b || !out) return;
    *out = b->stats;
}
