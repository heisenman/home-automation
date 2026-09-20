// Host unit test for the Broan frame codec (no ESP deps). Run via ./run.sh, or manually:
//   cc test/test_ha_broan_frame.c ha_broan_frame.c -Iinclude -o /tmp/t && /tmp/t
#include "ha_broan_frame.h"
#include <stdio.h>
#include <string.h>

static int fails = 0;
static void check(const char *name, int cond) {
    printf("%s  %s\n", cond ? "PASS" : "FAIL", name);
    if (!cond) fails++;
}
static int eq(const uint8_t *a, const uint8_t *b, size_t n) { return memcmp(a, b, n) == 0; }

int main(void) {
    uint8_t pay[BROAN_MAX_PAYLOAD];
    uint8_t frame[BROAN_MAX_FRAME];

    // ── checksum ──────────────────────────────────────────────────────────────
    // The canonical heartbeat: a zero-length write to register 0x0050, payload 40 00 50 00.
    //   total = 0x01 + 0x12 + 0x10 + 0x01 + 0x04 = 40, + 0x40+0x00+0x50+0x00 = 144  -> 184
    //   0xFF & (0 - (184 - 1)) = 0x49
    const uint8_t hb_payload[4] = { 0x40, 0x00, 0x50, 0x00 };
    check("checksum = 0x49",
          broan_checksum(BROAN_ADDR_CLIENT, BROAN_ADDR_ERV, hb_payload, 4) == 0x49);
    check("checksum is address-symmetric",
          broan_checksum(BROAN_ADDR_ERV, BROAN_ADDR_CLIENT, hb_payload, 4) ==
          broan_checksum(BROAN_ADDR_CLIENT, BROAN_ADDR_ERV, hb_payload, 4));
    // A single flipped payload bit must move it — the property the whole check exists for.
    uint8_t flipped[4] = { 0x40, 0x00, 0x51, 0x00 };
    check("checksum detects a bit flip",
          broan_checksum(BROAN_ADDR_CLIENT, BROAN_ADDR_ERV, flipped, 4) != 0x49);

    // ── heartbeat, byte for byte ──────────────────────────────────────────────
    int n = broan_build_heartbeat(pay, sizeof pay);
    check("heartbeat payload is 4 bytes", n == 4);
    check("  = 40 00 50 00", eq(pay, hb_payload, 4));

    int fn = broan_encode(frame, sizeof frame, BROAN_ADDR_ERV, BROAN_ADDR_CLIENT, pay, (uint8_t)n);
    const uint8_t want[11] = { 0x01, 0x10, 0x12, 0x01, 0x04, 0x40, 0x00, 0x50, 0x00, 0x49, 0x04 };
    check("heartbeat frame is 11 bytes", fn == 11);
    check("  = 01 10 12 01 04 40 00 50 00 49 04", eq(frame, want, 11));

    // ── round trip ────────────────────────────────────────────────────────────
    broan_frame_t f;
    size_t used = 0;
    check("decodes",            broan_decode(frame, (size_t)fn, &f, &used) == BROAN_DECODE_OK);
    check("  consumed all",     used == (size_t)fn);
    check("  target",           f.target == BROAN_ADDR_ERV);
    check("  sender",           f.sender == BROAN_ADDR_CLIENT);
    check("  len",              f.len == 4);
    check("  payload",          eq(f.payload, hb_payload, 4));

    // ── decoder robustness ────────────────────────────────────────────────────
    check("partial -> NEED_MORE",  broan_decode(frame, 4, &f, &used) == BROAN_DECODE_NEED_MORE);
    check("  consumes nothing",    used == 0);
    check("one short -> NEED_MORE",
          broan_decode(frame, (size_t)fn - 1, &f, &used) == BROAN_DECODE_NEED_MORE);
    check("empty -> NEED_MORE",    broan_decode(frame, 0, &f, &used) == BROAN_DECODE_NEED_MORE);

    uint8_t noisy[16];
    noisy[0] = 0xAA; noisy[1] = 0xBB;
    memcpy(&noisy[2], frame, 11);
    check("leading junk -> RESYNC", broan_decode(noisy, 13, &f, &used) == BROAN_DECODE_RESYNC);
    check("  skips to STX",         used == 2);
    check("  then decodes",         broan_decode(noisy + used, 11, &f, &used) == BROAN_DECODE_OK);

    uint8_t bad[11];
    memcpy(bad, frame, 11);
    bad[9] ^= 0xFF;                                   // corrupt the checksum
    check("bad checksum caught",    broan_decode(bad, 11, &f, &used) == BROAN_DECODE_BAD_CHECKSUM);
    check("  advances to resync",   used == 1);

    memcpy(bad, frame, 11);
    bad[7] ^= 0x01;                                   // corrupt a payload byte instead
    check("payload corruption caught", broan_decode(bad, 11, &f, &used) == BROAN_DECODE_BAD_CHECKSUM);

    memcpy(bad, frame, 11);
    bad[3] = 0x02;                                    // wrong alignment byte
    check("bad alignment caught",   broan_decode(bad, 11, &f, &used) == BROAN_DECODE_BAD_FRAMING);

    memcpy(bad, frame, 11);
    bad[10] = 0x00;                                   // wrong footer
    check("bad footer caught",      broan_decode(bad, 11, &f, &used) == BROAN_DECODE_BAD_FRAMING);

    // ── read requests ─────────────────────────────────────────────────────────
    const uint16_t regs[3] = { BROAN_REG_FAN_MODE, BROAN_REG_POWER_W, BROAN_REG_TEMP_SUPPLY };
    n = broan_build_read(pay, sizeof pay, regs, 3);
    const uint8_t want_read[7] = { 0x20, 0x00, 0x20, 0x23, 0x50, 0x01, 0xE0 };
    check("read req length",        n == 7);
    check("  high byte first",      eq(pay, want_read, 7));

    uint16_t many[BROAN_MAX_READ_REGS + 1] = {0};
    check("read req caps at 10",    broan_build_read(pay, sizeof pay, many, 11) == -1);
    check("  10 is allowed",        broan_build_read(pay, sizeof pay, many, 10) == 21);
    check("empty read req is just the opcode", broan_build_read(pay, sizeof pay, NULL, 0) == 1);

    // ── writes ────────────────────────────────────────────────────────────────
    n = broan_build_write_u8(pay, sizeof pay, BROAN_REG_FAN_MODE, BROAN_FAN_MIN);
    const uint8_t want_u8[5] = { 0x40, 0x00, 0x20, 0x01, 0x09 };
    check("write u8",               n == 5 && eq(pay, want_u8, 5));

    n = broan_build_write_i32(pay, sizeof pay, BROAN_REG_INTERMITTENT, 1000);
    const uint8_t want_i32[8] = { 0x40, 0x02, 0x22, 0x04, 0xE8, 0x03, 0x00, 0x00 };
    check("write i32 little-endian", n == 8 && eq(pay, want_i32, 8));

    // 72.5f is 0x42910000; on the wire that is little-endian 00 00 91 42.
    n = broan_build_write_f32(pay, sizeof pay, BROAN_REG_CTRL_HUMIDITY, 72.5f);
    const uint8_t want_f32[8] = { 0x40, 0x04, 0x50, 0x04, 0x00, 0x00, 0x91, 0x42 };
    check("write f32 little-endian", n == 8 && eq(pay, want_f32, 8));

    // ── TLV walking + value extraction ────────────────────────────────────────
    // A read response carrying fan mode (byte), power (float 72.5), and fault (-1 = healthy).
    const uint8_t resp[] = {
        0x21,
        0x00, 0x20, 0x01, 0x0B,                          // FAN_MODE = MANUAL
        0x23, 0x50, 0x04, 0x00, 0x00, 0x91, 0x42,        // POWER_W  = 72.5
        0x17, 0x00, 0x04, 0xFF, 0xFF, 0xFF, 0xFF,        // FAULT    = -1
    };
    size_t cur = 1;
    broan_tlv_t t;

    check("tlv 1",                  broan_tlv_next(resp, sizeof resp, &cur, &t));
    check("  reg = FAN_MODE",       t.reg == BROAN_REG_FAN_MODE);
    check("  = MANUAL",             broan_tlv_u8(&t) == BROAN_FAN_MANUAL);

    check("tlv 2",                  broan_tlv_next(resp, sizeof resp, &cur, &t));
    check("  reg = POWER_W",        t.reg == BROAN_REG_POWER_W);
    check("  = 72.5 W",             broan_tlv_f32(&t) == 72.5f);

    check("tlv 3",                  broan_tlv_next(resp, sizeof resp, &cur, &t));
    check("  reg = FAULT",          t.reg == BROAN_REG_FAULT);
    check("  = -1",                 broan_tlv_i32(&t) == -1);
    check("  reads as healthy",     broan_code_is_ok(broan_tlv_i32(&t)));

    check("tlv run ends",          !broan_tlv_next(resp, sizeof resp, &cur, &t));

    // A truncated run must stop, not walk off the end.
    const uint8_t trunc[] = { 0x21, 0x23, 0x50, 0x04, 0x00, 0x00 };   // claims 4 bytes, supplies 2
    cur = 1;
    check("truncated tlv rejected", !broan_tlv_next(trunc, sizeof trunc, &cur, &t));
    const uint8_t stub[] = { 0x21, 0x23 };                            // not even a full header
    cur = 1;
    check("stub tlv rejected",     !broan_tlv_next(stub, sizeof stub, &cur, &t));

    // ── guards ────────────────────────────────────────────────────────────────
    check("OVR is refused",        !broan_fan_mode_writable(BROAN_FAN_OVR));
    check("MIN is allowed",         broan_fan_mode_writable(BROAN_FAN_MIN));
    check("OFF is allowed",         broan_fan_mode_writable(BROAN_FAN_OFF));

    uint8_t tiny[3];
    check("encode respects cap",    broan_encode(tiny, sizeof tiny, 0x10, 0x12, hb_payload, 4) == -1);
    check("read respects cap",      broan_build_read(tiny, 2, regs, 3) == -1);
    check("write respects cap",     broan_build_write_f32(tiny, sizeof tiny, BROAN_REG_CTRL_HUMIDITY, 1.0f) == -1);

    // Zero-length payload is legal framing (the token ack is one byte, but prove the edge).
    fn = broan_encode(frame, sizeof frame, BROAN_ADDR_ERV, BROAN_ADDR_CLIENT, NULL, 0);
    check("empty payload encodes",  fn == 7);
    check("  and decodes",          broan_decode(frame, (size_t)fn, &f, &used) == BROAN_DECODE_OK &&
                                    f.len == 0);

    // ── ping/pong echo ────────────────────────────────────────────────────────
    const uint8_t ping[5] = { 0x02, 'P', 'i', 'n', 'g' };
    n = broan_build_pong(pay, sizeof pay, ping, 5);
    const uint8_t want_pong[5] = { 0x03, 'P', 'i', 'n', 'g' };
    check("pong echoes the payload", n == 5 && eq(pay, want_pong, 5));
    n = broan_build_token_ack(pay, sizeof pay);
    check("token ack",              n == 1 && pay[0] == BROAN_MSG_TOKEN_ACK);

    printf("\n%s (%d failure%s)\n", fails ? "FAILED" : "ALL PASS", fails, fails == 1 ? "" : "s");
    return fails ? 1 : 0;
}
