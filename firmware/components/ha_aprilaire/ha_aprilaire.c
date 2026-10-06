// ha_aprilaire — see include/ha_aprilaire.h. Pure C, no IDF dependency (host-tested: test/run.sh).
#include "ha_aprilaire.h"

#include <stdio.h>

uint8_t ha_apr_checksum(const uint8_t *body, size_t len) {
    uint32_t sum = HA_APR_STX;
    for (size_t i = 0; i < len; i++) sum += body[i];
    return (uint8_t)sum;
}

static int hexval(uint8_t c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

static int hex2(const uint8_t *p) {
    int hi = hexval(p[0]), lo = hexval(p[1]);
    return (hi < 0 || lo < 0) ? -1 : hi * 16 + lo;
}

bool ha_apr_parse_m(const uint8_t *body, size_t len, ha_apr_m_t *out) {
    // 'M' cmd rh[2] code[2] cs[2] = 8 bytes
    if (!body || !out || len != 8 || body[0] != 'M') return false;
    int cs = hex2(body + 6);
    if (cs < 0 || ha_apr_checksum(body, 6) != (uint8_t)cs) return false;
    if (body[1] != '?' && body[1] != '!') return false;
    int rh = hex2(body + 2), code = hex2(body + 4);
    if (rh < 0 || rh > 100 || code < 0) return false;
    out->running = (body[1] == '!');
    out->rh_pct = (uint8_t)rh;
    out->code = (uint8_t)code;
    return true;
}

size_t ha_apr_build_r(const ha_apr_r_t *r, uint8_t *out, size_t cap) {
    if (!r || !out || r->dryness < 1 || r->dryness > 7 || r->rh_x10 > 1000) return 0;
    char body[16];
    int bl = snprintf(body, sizeof body, "R%02X%02X%04X%02X%02X",
                      r->on ? 1u : 0u, (unsigned)r->dryness, (unsigned)r->rh_x10, 0x01u, 0x00u);
    if (bl != 13) return 0;
    size_t need = 1 + (size_t)bl + 2 + 1;
    if (cap < need + 1) return 0;   // +1 for snprintf's NUL
    uint8_t cs = ha_apr_checksum((const uint8_t *)body, (size_t)bl);
    int n = snprintf((char *)out, cap, "%c%s%02X%c", HA_APR_STX, body, cs, HA_APR_ETX);
    return n == (int)need ? need : 0;
}

bool ha_apr_feed(ha_apr_framer_t *f, uint8_t c) {
    if (c == HA_APR_STX) { f->in = true; f->len = 0; return false; }
    if (!f->in) return false;
    if (c == HA_APR_ETX) { f->in = false; return true; }
    if (f->len < HA_APR_MAX_BODY) f->body[f->len++] = c;
    else f->in = false;   // overlong: drop it, resync on the next STX
    return false;
}
