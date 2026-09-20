// ha_broan_frame — Broan AI Series ERV wire codec (ADR-0041). Pure: no ESP deps, host-tested.
// Verified against nspitko/broan_erv_uart. See include/ha_broan_frame.h for the contract.
#include "ha_broan_frame.h"

#include <string.h>

// Register opcodes go on the wire high byte first.
static inline uint8_t reg_hi(uint16_t reg) { return (uint8_t)(reg >> 8); }
static inline uint8_t reg_lo(uint16_t reg) { return (uint8_t)(reg & 0xFFu); }

bool broan_fan_mode_writable(uint8_t mode) {
    // OVR is a hard override that wedges the unit until cleared, and the reference implementation makes it
    // display-only for the same reason. Refusing it here means a bad automation cannot reach it at all.
    return mode != (uint8_t)BROAN_FAN_OVR;
}

uint8_t broan_checksum(uint8_t addr_a, uint8_t addr_b, const uint8_t *payload, uint8_t len) {
    // Verbatim from the reference:
    //     total = 0x01 + sender + receiver + 0x01 + message.size();
    //     for (b : message) total += b;
    //     return 0xFF & (0 - (total - 1));
    // All arithmetic is deliberately mod-256. Note it is NOT a CRC — years of community effort went into
    // trying Modbus CRCs against this bus before someone read the traffic.
    uint8_t total = (uint8_t)(BROAN_STX + addr_a + addr_b + BROAN_ALIGN + len);
    for (uint8_t i = 0; i < len; i++) total = (uint8_t)(total + payload[i]);
    return (uint8_t)(0u - (uint8_t)(total - 1u));
}

int broan_encode(uint8_t *out, size_t out_cap,
                 uint8_t target, uint8_t sender,
                 const uint8_t *payload, uint8_t len) {
    if (!out) return -1;
    if (len && !payload) return -1;
    size_t need = 5u + (size_t)len + 2u;
    if (out_cap < need) return -1;

    size_t i = 0;
    out[i++] = BROAN_STX;
    out[i++] = target;
    out[i++] = sender;
    out[i++] = BROAN_ALIGN;
    out[i++] = len;
    if (len) memcpy(&out[i], payload, len);
    i += len;
    out[i++] = broan_checksum(sender, target, payload, len);
    out[i++] = BROAN_ETX;
    return (int)i;
}

broan_decode_rc_t broan_decode(const uint8_t *buf, size_t len,
                               broan_frame_t *out, size_t *consumed) {
    if (consumed) *consumed = 0;
    if (!buf || !out) return BROAN_DECODE_BAD_FRAMING;

    // Resynchronise to the next STX. A bus we share with a chatty ERV will hand us partial frames after
    // any reset, and silently eating them beats wedging on the first stray byte.
    size_t start = 0;
    while (start < len && buf[start] != BROAN_STX) start++;
    if (start > 0) {
        if (consumed) *consumed = start;
        return BROAN_DECODE_RESYNC;
    }
    if (len < 5) return BROAN_DECODE_NEED_MORE;

    uint8_t target = buf[1];
    uint8_t sender = buf[2];
    uint8_t align  = buf[3];
    uint8_t plen   = buf[4];

    if (align != BROAN_ALIGN) {
        if (consumed) *consumed = 1;   // not a real frame start; step past this STX
        return BROAN_DECODE_BAD_FRAMING;
    }

    size_t total = 5u + (size_t)plen + 2u;
    if (len < total) return BROAN_DECODE_NEED_MORE;

    const uint8_t *payload = &buf[5];
    uint8_t want = broan_checksum(sender, target, payload, plen);
    if (buf[5 + plen] != want) {
        if (consumed) *consumed = 1;   // resync rather than trusting the length of a corrupt frame
        return BROAN_DECODE_BAD_CHECKSUM;
    }
    if (buf[5 + plen + 1] != BROAN_ETX) {
        if (consumed) *consumed = 1;
        return BROAN_DECODE_BAD_FRAMING;
    }

    out->target  = target;
    out->sender  = sender;
    out->len     = plen;
    out->payload = payload;
    if (consumed) *consumed = total;
    return BROAN_DECODE_OK;
}

int broan_build_pong(uint8_t *out, size_t cap, const uint8_t *ping_payload, uint8_t ping_len) {
    if (!out) return -1;
    // Echo everything after the ping's own opcode byte.
    uint8_t echo = (ping_len > 0) ? (uint8_t)(ping_len - 1u) : 0u;
    if (cap < 1u + (size_t)echo) return -1;
    out[0] = BROAN_MSG_PONG;
    if (echo) {
        if (!ping_payload) return -1;
        memcpy(&out[1], &ping_payload[1], echo);
    }
    return 1 + (int)echo;
}

int broan_build_token_ack(uint8_t *out, size_t cap) {
    if (!out || cap < 1) return -1;
    out[0] = BROAN_MSG_TOKEN_ACK;
    return 1;
}

int broan_build_read(uint8_t *out, size_t cap, const uint16_t *regs, size_t n) {
    if (!out || (n && !regs)) return -1;
    if (n > BROAN_MAX_READ_REGS) return -1;   // the ERV does not answer longer requests
    size_t need = 1u + n * 2u;
    if (cap < need) return -1;

    size_t i = 0;
    out[i++] = BROAN_MSG_READ_REQ;
    for (size_t r = 0; r < n; r++) {
        out[i++] = reg_hi(regs[r]);
        out[i++] = reg_lo(regs[r]);
    }
    return (int)i;
}

// One write TLV: opcode(2) | len(1) | data(len).
static int build_write(uint8_t *out, size_t cap, uint16_t reg, const uint8_t *data, uint8_t dlen) {
    size_t need = 1u + 3u + (size_t)dlen;
    if (!out || cap < need) return -1;
    size_t i = 0;
    out[i++] = BROAN_MSG_WRITE_REQ;
    out[i++] = reg_hi(reg);
    out[i++] = reg_lo(reg);
    out[i++] = dlen;
    for (uint8_t b = 0; b < dlen; b++) out[i++] = data[b];
    return (int)i;
}

int broan_build_heartbeat(uint8_t *out, size_t cap) {
    // A zero-length write to 0x5000. On the wire: 40 00 50 00.
    return build_write(out, cap, BROAN_REG_HEARTBEAT, NULL, 0);
}

int broan_build_write_u8(uint8_t *out, size_t cap, uint16_t reg, uint8_t value) {
    return build_write(out, cap, reg, &value, 1);
}

int broan_build_write_i32(uint8_t *out, size_t cap, uint16_t reg, int32_t value) {
    uint32_t u = (uint32_t)value;
    uint8_t d[4] = { (uint8_t)(u & 0xFFu), (uint8_t)((u >> 8) & 0xFFu),
                     (uint8_t)((u >> 16) & 0xFFu), (uint8_t)((u >> 24) & 0xFFu) };
    return build_write(out, cap, reg, d, 4);
}

int broan_build_write_f32(uint8_t *out, size_t cap, uint16_t reg, float value) {
    uint32_t u;
    memcpy(&u, &value, 4);           // reinterpret the float's bits, host order
    uint8_t d[4] = { (uint8_t)(u & 0xFFu), (uint8_t)((u >> 8) & 0xFFu),
                     (uint8_t)((u >> 16) & 0xFFu), (uint8_t)((u >> 24) & 0xFFu) };
    return build_write(out, cap, reg, d, 4);   // …emitted little-endian regardless of host order
}

bool broan_tlv_next(const uint8_t *payload, uint8_t len, size_t *cursor, broan_tlv_t *out) {
    if (!payload || !cursor || !out) return false;
    size_t i = *cursor;
    if (i + 3u > (size_t)len) return false;        // no room for opcode + length

    uint16_t reg  = (uint16_t)((uint16_t)payload[i] << 8 | payload[i + 1]);
    uint8_t  dlen = payload[i + 2];
    if (i + 3u + (size_t)dlen > (size_t)len) return false;   // truncated run

    out->reg  = reg;
    out->len  = dlen;
    out->data = dlen ? &payload[i + 3] : NULL;
    *cursor   = i + 3u + (size_t)dlen;
    return true;
}

uint8_t broan_tlv_u8(const broan_tlv_t *t) {
    return (t && t->data && t->len >= 1) ? t->data[0] : 0u;
}

int32_t broan_tlv_i32(const broan_tlv_t *t) {
    if (!t || !t->data || t->len < 4) return 0;
    uint32_t u = (uint32_t)t->data[0]
               | ((uint32_t)t->data[1] << 8)
               | ((uint32_t)t->data[2] << 16)
               | ((uint32_t)t->data[3] << 24);
    return (int32_t)u;
}

float broan_tlv_f32(const broan_tlv_t *t) {
    if (!t || !t->data || t->len < 4) return 0.0f;
    uint32_t u = (uint32_t)t->data[0]
               | ((uint32_t)t->data[1] << 8)
               | ((uint32_t)t->data[2] << 16)
               | ((uint32_t)t->data[3] << 24);
    float f;
    memcpy(&f, &u, 4);
    return f;
}
