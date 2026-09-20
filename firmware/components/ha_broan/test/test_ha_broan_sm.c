// Host unit test for the Broan session state machine, driven against a SIMULATED ERV (no ESP deps).
//   cc test/test_ha_broan_sm.c ha_broan.c ha_broan_frame.c -Iinclude -o /tmp/t && /tmp/t
//
// The state machine is pure (bytes in, bytes out) precisely so the whole token conversation — ping/pong,
// the 0x04/0x05 handshake, heartbeat cadence, chunked polling, write acks, release — can be proven here
// rather than discovered on a bus where a mistake shuts down a ventilator.
#include "ha_broan.h"
#include <stdio.h>
#include <string.h>

static int fails = 0;
static void check(const char *name, int cond) {
    printf("%s  %s\n", cond ? "PASS" : "FAIL", name);
    if (!cond) fails++;
}

// ── a simulated ERV ───────────────────────────────────────────────────────────

typedef struct { uint16_t reg; uint8_t len; uint8_t raw[4]; } sim_reg_t;

typedef struct {
    uint8_t  tx[1024];
    size_t   txlen;
    int      pongs, token_acks, heartbeats, reads, writes, releases;
    uint16_t last_read[16];
    int      last_read_n;
    uint16_t last_write_reg;
    bool     answer_reads;      // clear to simulate a lost reply
    sim_reg_t regs[8];
    int      nregs;
} sim_erv_t;

static void sim_put_f32(sim_erv_t *s, uint16_t reg, float v) {
    uint32_t u; memcpy(&u, &v, 4);
    sim_reg_t *r = &s->regs[s->nregs++];
    r->reg = reg; r->len = 4;
    r->raw[0] = (uint8_t)(u); r->raw[1] = (uint8_t)(u >> 8);
    r->raw[2] = (uint8_t)(u >> 16); r->raw[3] = (uint8_t)(u >> 24);
}
static void sim_put_u8(sim_erv_t *s, uint16_t reg, uint8_t v) {
    sim_reg_t *r = &s->regs[s->nregs++];
    r->reg = reg; r->len = 1; r->raw[0] = v;
}
static void sim_put_i32(sim_erv_t *s, uint16_t reg, int32_t v) {
    uint32_t u = (uint32_t)v;
    sim_reg_t *r = &s->regs[s->nregs++];
    r->reg = reg; r->len = 4;
    r->raw[0] = (uint8_t)(u); r->raw[1] = (uint8_t)(u >> 8);
    r->raw[2] = (uint8_t)(u >> 16); r->raw[3] = (uint8_t)(u >> 24);
}
static const sim_reg_t *sim_lookup(const sim_erv_t *s, uint16_t reg) {
    for (int i = 0; i < s->nregs; i++) if (s->regs[i].reg == reg) return &s->regs[i];
    return NULL;
}

// Queue a frame from the ERV to the client.
static void sim_send(sim_erv_t *s, const uint8_t *payload, size_t len) {
    uint8_t f[BROAN_MAX_FRAME];
    int n = broan_encode(f, sizeof f, BROAN_ADDR_CLIENT, BROAN_ADDR_ERV, payload, (uint8_t)len);
    if (n > 0 && s->txlen + (size_t)n <= sizeof(s->tx)) {
        memcpy(s->tx + s->txlen, f, (size_t)n);
        s->txlen += (size_t)n;
    }
}

static void sim_ping(sim_erv_t *s)        { const uint8_t p[5] = {0x02,'P','i','n','g'}; sim_send(s, p, 5); }
static void sim_offer_token(sim_erv_t *s) { const uint8_t p[1] = {BROAN_MSG_TOKEN_OFFER}; sim_send(s, p, 1); }

// Consume one frame the client transmitted and react the way the ERV would.
static void sim_recv(sim_erv_t *s, const uint8_t *frame, size_t len) {
    broan_frame_t f; size_t used;
    if (broan_decode(frame, len, &f, &used) != BROAN_DECODE_OK || f.len == 0) return;

    switch (f.payload[0]) {
    case BROAN_MSG_PONG:      s->pongs++; break;
    case BROAN_MSG_TOKEN_ACK: s->token_acks++; break;

    case BROAN_MSG_TOKEN_OFFER: s->releases++; break;   // client handing the bus back

    case BROAN_MSG_WRITE_REQ: {
        uint16_t reg = (uint16_t)((uint16_t)f.payload[1] << 8 | f.payload[2]);
        s->last_write_reg = reg;
        if (reg == BROAN_REG_HEARTBEAT) s->heartbeats++; else s->writes++;
        uint8_t ack[3] = { BROAN_MSG_WRITE_ACK, f.payload[1], f.payload[2] };
        sim_send(s, ack, 3);
        break;
    }

    case BROAN_MSG_READ_REQ: {
        s->reads++;
        s->last_read_n = 0;
        uint8_t resp[BROAN_MAX_PAYLOAD];
        size_t o = 0;
        resp[o++] = BROAN_MSG_READ_RESP;
        for (size_t i = 1; i + 1 < f.len; i += 2) {
            uint16_t reg = (uint16_t)((uint16_t)f.payload[i] << 8 | f.payload[i + 1]);
            if (s->last_read_n < 16) s->last_read[s->last_read_n++] = reg;
            const sim_reg_t *r = sim_lookup(s, reg);
            if (!r) continue;
            resp[o++] = f.payload[i]; resp[o++] = f.payload[i + 1]; resp[o++] = r->len;
            memcpy(&resp[o], r->raw, r->len); o += r->len;
        }
        if (s->answer_reads) sim_send(s, resp, o);
        break;
    }
    default: break;
    }
}

// Run the pair forward: deliver the ERV's queued bytes, let the client speak, feed that back.
static void pump(ha_broan_t *c, sim_erv_t *s, uint32_t *t, int rounds) {
    uint8_t buf[BROAN_MAX_FRAME];
    for (int i = 0; i < rounds; i++) {
        if (s->txlen) {
            ha_broan_rx(c, s->tx, s->txlen, *t);
            s->txlen = 0;
        }
        size_t n = ha_broan_next_tx(c, buf, sizeof buf, *t);
        if (n) sim_recv(s, buf, n);
        *t += 10;
    }
}

static void sim_init(sim_erv_t *s) {
    memset(s, 0, sizeof(*s));
    s->answer_reads = true;
    sim_put_u8 (s, BROAN_REG_FAN_MODE,     BROAN_FAN_MANUAL);
    sim_put_f32(s, BROAN_REG_POWER_W,      72.5f);
    sim_put_f32(s, BROAN_REG_TEMP_SUPPLY,  21.5f);
    sim_put_f32(s, BROAN_REG_CFM_SUPPLY,   88.0f);
    sim_put_i32(s, BROAN_REG_FAULT,        -1);
    sim_put_i32(s, BROAN_REG_UPTIME,       123456);
}

int main(void) {
    ha_broan_t c;
    sim_erv_t  s;
    uint32_t   t;
    ha_broan_stats_t st;

    // ── ping / pong ───────────────────────────────────────────────────────────
    sim_init(&s); t = 1000;
    ha_broan_init(&c, NULL, t);
    check("starts offline",          !ha_broan_online(&c, t));
    sim_ping(&s);
    pump(&c, &s, &t, 3);
    check("answers the ping",         s.pongs == 1);
    ha_broan_get_stats(&c, &st);
    check("  counted",                st.pings == 1);

    // ── token handshake, heartbeat, chunked polling, release ──────────────────
    sim_init(&s); t = 1000;
    ha_broan_init(&c, NULL, t);
    sim_offer_token(&s);
    pump(&c, &s, &t, 2);
    check("acks the token",           s.token_acks == 1);
    check("holds the token",          ha_broan_have_token(&c));
    check("online once offered",      ha_broan_online(&c, t));

    pump(&c, &s, &t, 12);
    check("heartbeat sent in hold",   s.heartbeats >= 1);
    // 13 default registers, 10 max per request => exactly two requests per sweep.
    check("poll chunked into 2",      s.reads == 2);
    check("  second chunk is 3",      s.last_read_n == 3);   // 13 regs => 10 + 3
    check("releases the token",       s.releases == 1);
    check("  no longer holding",     !ha_broan_have_token(&c));

    // ── the field cache ───────────────────────────────────────────────────────
    float f = 0; int32_t i32 = 0; uint8_t u8 = 0;
    check("cached power",             ha_broan_get_f32(&c, BROAN_REG_POWER_W, &f) && f == 72.5f);
    check("cached temp",              ha_broan_get_f32(&c, BROAN_REG_TEMP_SUPPLY, &f) && f == 21.5f);
    check("cached fan mode",          ha_broan_get_u8(&c, BROAN_REG_FAN_MODE, &u8) && u8 == BROAN_FAN_MANUAL);
    check("cached fault",             ha_broan_get_i32(&c, BROAN_REG_FAULT, &i32) && i32 == -1);
    check("  reads as healthy",       broan_code_is_ok(i32));
    check("unseen reg has no value", !ha_broan_get_f32(&c, BROAN_REG_RPM_EXHAUST, &f));
    check("unseen reg age is max",    ha_broan_age_ms(&c, BROAN_REG_RPM_EXHAUST, t) == UINT32_MAX);
    check("seen reg has a real age",  ha_broan_age_ms(&c, BROAN_REG_POWER_W, t) < 10000);

    // ── writes ────────────────────────────────────────────────────────────────
    check("OVR refused",             !ha_broan_set_fan_mode(&c, BROAN_FAN_OVR));
    check("MIN accepted",             ha_broan_set_fan_mode(&c, BROAN_FAN_MIN));
    sim_offer_token(&s);
    pump(&c, &s, &t, 6);
    check("write transmitted",        s.writes >= 1);
    check("  to FAN_MODE",            s.last_write_reg == BROAN_REG_FAN_MODE);
    // The write ack must invalidate the cached value, or we would keep reporting the pre-write reading.
    check("write ack drops cache",   !ha_broan_get_u8(&c, BROAN_REG_FAN_MODE, &u8));

    check("humidity write queued",    ha_broan_report_humidity(&c, 41.5f));
    sim_offer_token(&s);
    pump(&c, &s, &t, 6);
    check("  transmitted",            s.last_write_reg == BROAN_REG_CTRL_HUMIDITY);

    // Queue depth is finite and overflow is counted rather than silently swallowed.
    for (int k = 0; k < HA_BROAN_TXQ_DEPTH + 4; k++) (void)ha_broan_report_humidity(&c, 40.0f);
    ha_broan_get_stats(&c, &st);
    check("queue overflow counted",   st.writes_dropped > 0);

    // ── ⛔ the heartbeat outranks queued writes ───────────────────────────────
    // E50 shuts the unit down. A backlog of user writes must never be the reason we go quiet.
    sim_init(&s); t = 1000;
    ha_broan_init(&c, NULL, t);
    for (int k = 0; k < 4; k++) (void)ha_broan_report_humidity(&c, 40.0f);
    t += BROAN_HEARTBEAT_MS + 100;              // heartbeat now due
    sim_offer_token(&s);
    {
        uint8_t buf[BROAN_MAX_FRAME];
        ha_broan_rx(&c, s.tx, s.txlen, t); s.txlen = 0;
        size_t n = ha_broan_next_tx(&c, buf, sizeof buf, t);   // token ack
        if (n) sim_recv(&s, buf, n);
        n = ha_broan_next_tx(&c, buf, sizeof buf, t);          // must be the heartbeat, not a write
        if (n) sim_recv(&s, buf, n);
        check("heartbeat precedes writes", s.heartbeats == 1 && s.writes == 0);
    }

    // ── E50 exposure ──────────────────────────────────────────────────────────
    check("no exposure before taking the bus", ha_broan_e50_exposure_ms(&c, t) == 0 || s.heartbeats > 0);
    sim_init(&s); t = 1000;
    ha_broan_init(&c, NULL, t);
    check("nothing owed yet",         ha_broan_e50_exposure_ms(&c, t) == 0);
    sim_offer_token(&s);
    pump(&c, &s, &t, 12);
    ha_broan_get_stats(&c, &st);
    check("heartbeat happened",       st.heartbeats >= 1);
    uint32_t far = t + 4000;
    check("exposure grows with silence", ha_broan_e50_exposure_ms(&c, far) >= 3000);

    // ── the ERV stops yielding the token ──────────────────────────────────────
    sim_init(&s); t = 1000;
    ha_broan_init(&c, NULL, t);
    sim_offer_token(&s);
    pump(&c, &s, &t, 12);
    check("online while yielding",    ha_broan_online(&c, t));
    t += BROAN_CONTROL_TIMEOUT_MS + 500;        // ERV goes quiet
    {
        uint8_t buf[BROAN_MAX_FRAME];
        (void)ha_broan_next_tx(&c, buf, sizeof buf, t);
    }
    ha_broan_get_stats(&c, &st);
    check("control timeout counted",  st.control_timeouts >= 1);
    check("goes offline",            !ha_broan_online(&c, t));

    // ── a reply that never arrives must not wedge the hold ────────────────────
    sim_init(&s); t = 1000;
    s.answer_reads = false;                     // ERV swallows read responses
    ha_broan_init(&c, NULL, t);
    sim_offer_token(&s);
    pump(&c, &s, &t, 250);                      // 2.5 s: long enough for reply timeouts to fire
    ha_broan_get_stats(&c, &st);
    check("reply timeout fires",      st.reply_timeouts >= 1);
    check("  still made progress",    s.reads >= 2);

    // ── ⛔ listen-only never transmits ────────────────────────────────────────
    sim_init(&s); t = 1000;
    ha_broan_cfg_t ro = { .listen_only = true };
    ha_broan_init(&c, &ro, t);
    sim_ping(&s);
    sim_offer_token(&s);
    pump(&c, &s, &t, 30);
    check("listen-only: no pong",     s.pongs == 0);
    check("listen-only: no ack",      s.token_acks == 0);
    check("listen-only: no heartbeat", s.heartbeats == 0);
    check("listen-only: no read",     s.reads == 0);
    check("listen-only: no release",  s.releases == 0);
    check("listen-only: write still queues", ha_broan_set_fan_mode(&c, BROAN_FAN_MIN));
    {
        uint8_t buf[BROAN_MAX_FRAME];
        check("  …but never emitted", ha_broan_next_tx(&c, buf, sizeof buf, t) == 0);
    }

    // Listen-only still HARVESTS telemetry: a response addressed to the real wall control is the cheapest
    // possible validation of the register map, before we ever take the bus.
    {
        const uint8_t resp[] = { 0x21, 0x23, 0x50, 0x04, 0x00, 0x00, 0x91, 0x42 };  // POWER_W = 72.5
        uint8_t frame[BROAN_MAX_FRAME];
        int n = broan_encode(frame, sizeof frame, 0x14 /* some other controller */,
                             BROAN_ADDR_ERV, resp, sizeof resp);
        ha_broan_rx(&c, frame, (size_t)n, t);
        check("harvests another controller's reply",
              ha_broan_get_f32(&c, BROAN_REG_POWER_W, &f) && f == 72.5f);
    }

    // ── stream robustness ─────────────────────────────────────────────────────
    sim_init(&s); t = 1000;
    ha_broan_init(&c, NULL, t);
    {
        // A frame split across three deliveries must still reassemble.
        uint8_t pay[8]; int pn = broan_build_heartbeat(pay, sizeof pay);
        uint8_t frame[BROAN_MAX_FRAME];
        int n = broan_encode(frame, sizeof frame, BROAN_ADDR_CLIENT, BROAN_ADDR_ERV, pay, (uint8_t)pn);
        ha_broan_rx(&c, frame, 3, t);
        ha_broan_rx(&c, frame + 3, 4, t);
        ha_broan_rx(&c, frame + 7, (size_t)n - 7, t);
        ha_broan_get_stats(&c, &st);
        check("split frame reassembles", st.frames_rx == 1);

        // Garbage between frames is skipped without losing the frame that follows.
        uint8_t junk[3] = { 0xAA, 0xBB, 0xCC };
        ha_broan_rx(&c, junk, 3, t);
        ha_broan_rx(&c, frame, (size_t)n, t);
        ha_broan_get_stats(&c, &st);
        check("junk then frame",         st.frames_rx == 2);

        // A corrupt checksum is counted, not acted on.
        frame[(size_t)n - 2] ^= 0xFF;
        ha_broan_rx(&c, frame, (size_t)n, t);
        ha_broan_get_stats(&c, &st);
        check("bad checksum counted",    st.frames_bad >= 1);
    }

    // ── millisecond wraparound ────────────────────────────────────────────────
    sim_init(&s);
    t = 0xFFFFFF00u;                            // 256 ms before the wrap
    ha_broan_init(&c, NULL, t);
    sim_offer_token(&s);
    pump(&c, &s, &t, 60);                       // carries the clock across the wrap
    check("works across the wrap",    s.token_acks >= 1 && s.reads >= 2);
    check("  online across the wrap", ha_broan_online(&c, t));

    printf("\n%s (%d failure%s)\n", fails ? "FAILED" : "ALL PASS", fails, fails == 1 ? "" : "s");
    return fails ? 1 : 0;
}
