// BREADCRUMB: firmware/components > ha_broan - Broan AI Series ERV wire protocol (RS-485). This header is the pure frame codec: framing, checksum, register encode/decode. Contract: ADR-0041. Parent: firmware/AGENTS.md.
// REUSE-WHEN: talking to a Broan / NuTone / Venmar / vanEE / Best AI-Series (AM1 platform) HRV or ERV over
// its D+/D- wall-control bus. NOT Modbus — see below.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// ⛔ THIS IS NOT MODBUS. Broan's 52-page installer manual never mentions Modbus, RS-485, or baud rate, and
// Broan declined to publish a spec. The Home Assistant community spent years cycling baud/parity against a
// CRC that does not exist — the checksum here is an 8-bit sum with deliberate overflow. Everything in this
// file was verified against nspitko/broan_erv_uart, the reverse-engineered reference implementation.
//
// Transport is 38400 8N1, half-duplex, and **token-passed rather than master/slave polled**: the ERV
// offers the bus with 0x04 and we may only transmit while we hold it. See ha_broan (state machine) — this
// header is only the codec, and it is pure so it can be proven on the host.
//
//   frame:  0x01 | target | sender | 0x01 | len | payload[len] | checksum | 0x04
//
// ⚠ The ERV tolerates exactly one controller and it is the WALL CONTROL's slot we take. Once we answer,
// we owe it a heartbeat every 10 s (BROAN_HEARTBEAT_MS); going quiet past BROAN_CONTROL_TIMEOUT_MS raises
// fault E50 and the unit SHUTS DOWN. That includes every OTA. See ADR-0041 §1.8.

#define BROAN_STX   0x01u
#define BROAN_ALIGN 0x01u
#define BROAN_ETX   0x04u

#define BROAN_ADDR_ERV    0x10u   // the unit
#define BROAN_ADDR_CLIENT 0x12u   // us (any address <= 32 appears legal)

// Message opcodes — payload[0].
#define BROAN_MSG_PING        0x02u   // ERV -> us, payload ASCII "Ping"
#define BROAN_MSG_PONG        0x03u   // our reply, echoing the rest of the ping payload
#define BROAN_MSG_TOKEN_OFFER 0x04u   // ERV offers the bus
#define BROAN_MSG_TOKEN_ACK   0x05u   // we take it
#define BROAN_MSG_READ_REQ    0x20u   // 0x20 | (hi lo) x N      — max 10 registers per request
#define BROAN_MSG_READ_RESP   0x21u   // 0x21 | (hi lo len data) x N
#define BROAN_MSG_WRITE_REQ   0x40u   // 0x40 | (hi lo len data) x N
#define BROAN_MSG_WRITE_ACK   0x41u

#define BROAN_HEARTBEAT_MS       10000u
#define BROAN_CONTROL_TIMEOUT_MS  5000u
#define BROAN_MAX_READ_REGS         10u

#define BROAN_MAX_PAYLOAD 255u
#define BROAN_MAX_FRAME   (5u + BROAN_MAX_PAYLOAD + 2u)

// ── registers (16-bit: high byte first on the wire) ────────────────────────────
// Tier A only — read from the reference implementation's field table. The full map, including the
// contested and do-not-write registers, is in docs/design/hvac-shade-device-integration.md §1.4.
// ⚠ These are (high << 8 | low) — the two wire bytes concatenated LEFT TO RIGHT, in the order they are
// transmitted. The design doc lists them as "HH LL" for exactly this reason. Do not byte-swap them to
// match the little-endian *values*: the opcode is big-endian on the wire, the payload is little-endian.
#define BROAN_REG_FAN_MODE        0x0020u  // R/W byte  — enum below
#define BROAN_REG_BASE_MODE       0x0220u  // R   i32
#define BROAN_REG_ACTIVE_MODE     0x0720u  // R   i32   0=Idle 1=Running 2=Max 3=Turbo 4=Manual
#define BROAN_REG_HUMIDITY_MODE   0x0F22u  // R/W byte
#define BROAN_REG_INTERMITTENT    0x0222u  // R/W i32   seconds on per hour
#define BROAN_REG_TARGET_RH_A     0x0C22u  // R/W f32   %RH — write together with B
#define BROAN_REG_TARGET_RH_B     0x0A22u  // R/W f32   %RH — write together with A
#define BROAN_REG_UPTIME          0x1400u  // R   i32   seconds
#define BROAN_REG_POWER_W         0x2350u  // R   f32   watts
#define BROAN_REG_TEMP_SUPPLY     0x01E0u  // R   f32   (reads high, per upstream)
#define BROAN_REG_TEMP_EXHAUST    0x03E0u  // R   f32   NaN without the 2nd thermistor
#define BROAN_REG_CFM_SUPPLY      0x0510u  // R   f32
#define BROAN_REG_CFM_EXHAUST     0x0610u  // R   f32
#define BROAN_REG_RPM_SUPPLY      0x0310u  // R   f32
#define BROAN_REG_RPM_EXHAUST     0x0410u  // R   f32
#define BROAN_REG_TARGET_CFM_IN   0x0622u  // R/W f32   the MED/manual speed setpoint (supply)
#define BROAN_REG_TARGET_CFM_OUT  0x0822u  // R/W f32   (exhaust)
#define BROAN_REG_MAX_CFM_IN      0x0E50u  // R/W f32
#define BROAN_REG_MAX_CFM_OUT     0x0F50u  // R/W f32
#define BROAN_REG_MIN_CFM_IN      0x0A50u  // R/W f32
#define BROAN_REG_MIN_CFM_OUT     0x0B50u  // R/W f32
#define BROAN_REG_HEARTBEAT       0x0050u  // W   void  — every 10 s, zero-length
#define BROAN_REG_CTRL_HUMIDITY   0x0450u  // W   f32   WE supply RH; the ERV has no humidity sensor
#define BROAN_REG_CTRL_TEMP       0x0550u  // W   f32
#define BROAN_REG_FILTER_RESET    0x0130u  // W   byte  0x01 = reset (preload FILTER_STAGE first!)
#define BROAN_REG_FILTER_LIFE     0x0830u  // R/W i32   seconds remaining
#define BROAN_REG_FILTER_STAGE    0x0930u  // W   i32   seconds — MUST be written before FILTER_RESET
#define BROAN_REG_FAULT           0x1700u  // R   i32   -1 / 0xFFFFFFFF = OK
#define BROAN_REG_WARNING         0x1A00u  // R   i32   -1 = OK; cycles through multiple actives
#define BROAN_REG_MODEL           0x0260u  // R   str   e.g. "AM1G4"
#define BROAN_REG_FW_NAME         0x0200u  // R   str   e.g. "am_main"
#define BROAN_REG_FW_VERSION      0x0100u  // R   str   3 bytes, DECIMAL not BCD
#define BROAN_REG_HW_REVISION     0x0160u  // R   str   3 bytes, decimal

// Fan mode enum for BROAN_REG_FAN_MODE.
typedef enum {
    BROAN_FAN_OFF          = 0x01,  // LCD "STB"
    BROAN_FAN_OVR          = 0x02,  // ⛔ NEVER WRITE — hard override, wedges the unit until cleared
    BROAN_FAN_RECIRCULATE  = 0x06,  // only on units with the J6 recirculation damper — not all have it
    BROAN_FAN_INTERMITTENT = 0x08,
    BROAN_FAN_MIN          = 0x09,
    BROAN_FAN_MAX          = 0x0A,
    BROAN_FAN_MANUAL       = 0x0B,  // LCD "MED"; pairs with TARGET_CFM_IN/OUT
    BROAN_FAN_TURBO        = 0x0C,
    BROAN_FAN_HUMIDITY     = 0x0D,
    BROAN_FAN_AWAY         = 0x0F,
    BROAN_FAN_SMART        = 0x11,
    // LCD shows AUT and DEF too; neither has a known register value. Do not guess — see ADR-0041.
} broan_fan_mode_t;

// True for modes this module refuses to write. Currently just OVR.
bool broan_fan_mode_writable(uint8_t mode);

// ── codec ─────────────────────────────────────────────────────────────────────

typedef enum {
    BROAN_DECODE_OK = 0,
    BROAN_DECODE_NEED_MORE,   // a frame may still be arriving; consume nothing, call again with more
    BROAN_DECODE_RESYNC,      // leading bytes are not a frame start; skip *consumed and retry
    BROAN_DECODE_BAD_CHECKSUM,
    BROAN_DECODE_BAD_FRAMING, // length or footer wrong
} broan_decode_rc_t;

typedef struct {
    uint8_t target;
    uint8_t sender;
    uint8_t len;
    const uint8_t *payload;   // points INTO the caller's buffer — no copy, no ownership
} broan_frame_t;

typedef struct {
    uint16_t reg;
    uint8_t  len;             // 1 = byte, 4 = i32/f32, other = string/unknown
    const uint8_t *data;      // points into the frame payload
} broan_tlv_t;

// The checksum, verbatim from the reference: an 8-bit sum with deliberate overflow, NOT a CRC.
// Symmetric in the two addresses (they are simply added), so argument order does not affect the result.
uint8_t broan_checksum(uint8_t addr_a, uint8_t addr_b, const uint8_t *payload, uint8_t len);

// Wrap a payload into a frame. Returns bytes written, or -1 if it would not fit.
int broan_encode(uint8_t *out, size_t out_cap,
                 uint8_t target, uint8_t sender,
                 const uint8_t *payload, uint8_t len);

// Find and validate one frame at the head of `buf`. On any return, *consumed says how many bytes to drop
// from the front of the stream (0 for NEED_MORE). `out->payload` aliases `buf` — copy it if you keep it.
broan_decode_rc_t broan_decode(const uint8_t *buf, size_t len,
                               broan_frame_t *out, size_t *consumed);

// ── message builders — all return payload length, or -1 if it would not fit ────
// These build the PAYLOAD; pass the result to broan_encode().

int broan_build_pong(uint8_t *out, size_t cap, const uint8_t *ping_payload, uint8_t ping_len);
int broan_build_token_ack(uint8_t *out, size_t cap);
int broan_build_read(uint8_t *out, size_t cap, const uint16_t *regs, size_t n);  // n <= BROAN_MAX_READ_REGS
int broan_build_heartbeat(uint8_t *out, size_t cap);
int broan_build_write_u8(uint8_t *out, size_t cap, uint16_t reg, uint8_t value);
int broan_build_write_i32(uint8_t *out, size_t cap, uint16_t reg, int32_t value);
int broan_build_write_f32(uint8_t *out, size_t cap, uint16_t reg, float value);

// ── TLV walking (read responses, 0x21, and write requests) ─────────────────────
// Iterate the (reg, len, data) triples in a payload. Pass *cursor = 1 to skip the opcode byte.
// Returns false at the end or on a malformed run.
bool broan_tlv_next(const uint8_t *payload, uint8_t len, size_t *cursor, broan_tlv_t *out);

// Little-endian value extraction. Explicit shifts rather than a union punt, so the codec is correct on a
// big-endian host too — which is what makes the test suite meaningful.
uint8_t broan_tlv_u8(const broan_tlv_t *t);
int32_t broan_tlv_i32(const broan_tlv_t *t);
float   broan_tlv_f32(const broan_tlv_t *t);

// Fault/warning registers read -1 (0xFFFFFFFF) when healthy.
static inline bool broan_code_is_ok(int32_t code) { return code == -1; }
