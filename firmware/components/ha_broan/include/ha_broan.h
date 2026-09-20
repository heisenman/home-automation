// BREADCRUMB: firmware/components > ha_broan - ERV session state machine: token handshake, ping/pong, heartbeat, chunked register polling, field cache. Pure (bytes in / bytes out), host-tested against a simulated ERV. Contract: ADR-0041. Parent: firmware/AGENTS.md.
// REUSE-WHEN: you need the Broan conversation, not just the framing. Sits on ha_broan_frame (codec) and is
// driven by ha_rs485 (transport) — but depends on neither, which is what makes it testable off-target.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "ha_broan_frame.h"

// The bus is TOKEN-PASSED, not polled. The ERV offers control with 0x04; we ack with 0x05, send ONE
// message at a time waiting for each reply, then hand the token back with 0x04. Everything below exists
// to get that dance right without wedging.
//
// ⛔ THE THING THIS MODULE IS REALLY FOR. The ERV tolerates one controller, and taking the bus means
// taking the WALL CONTROL's slot. From then on we owe it a heartbeat every 10 s; going quiet past the
// control timeout raises fault E50 and THE UNIT SHUTS DOWN — including on every OTA. So:
//   * the heartbeat outranks user writes and polling inside a token hold (see ha_broan_next_tx),
//   * ha_broan_e50_exposure_ms() reports how close we are, so a caller can alarm before the ERV faults,
//   * listen_only is honoured here as well as in the transport — defence in depth on a bus where a
//     spurious write reaches a ventilator.
//
// Whether E50 self-clears on reconnect or needs a manual power cycle is UNRESOLVED and blocking
// (ADR-0041 §1.8). Until it is answered, treat every deliberate disconnect as potentially requiring a
// physical trip.

#define HA_BROAN_RXBUF       512u   // stream reassembly; ~2 max frames
#define HA_BROAN_MAX_FIELDS   24u   // tracked registers
#define HA_BROAN_TXQ_DEPTH     8u   // queued control writes
#define HA_BROAN_MSG_MAX      16u   // a single write payload is 1 + 3 + 4 bytes

typedef struct {
    uint8_t  our_addr;             // 0 => BROAN_ADDR_CLIENT
    uint8_t  erv_addr;             // 0 => BROAN_ADDR_ERV

    // ⛔ Never transmit. Mirrors the ha_rs485 gate deliberately: bring-up step 1 is to sniff the ERV's
    // real conversation with its wall control still attached, and two independent gates is the right
    // number for a bus where an accidental write reaches a ventilator.
    bool     listen_only;

    uint32_t heartbeat_ms;         // 0 => BROAN_HEARTBEAT_MS (10000)
    uint32_t control_timeout_ms;   // 0 => BROAN_CONTROL_TIMEOUT_MS (5000)
    uint32_t poll_ms;              // 0 => 5000; how often a full register sweep is refreshed
    uint32_t reply_timeout_ms;     // 0 => 1000; give up waiting mid-hold rather than idling

    // Registers to poll. NULL => a sensible telemetry default (mode, power, temps, CFM, RPM, filter,
    // fault, warning, uptime). Automatically chunked to BROAN_MAX_READ_REGS per request.
    const uint16_t *poll_regs;
    uint8_t         poll_reg_count;
} ha_broan_cfg_t;

typedef struct {
    uint16_t reg;
    uint8_t  len;
    uint8_t  raw[4];
    uint32_t updated_ms;
    bool     valid;
} ha_broan_field_t;

typedef struct {
    uint32_t frames_rx;
    uint32_t frames_bad;        // checksum/framing rejects
    uint32_t pings;
    uint32_t tokens;            // token offers accepted
    uint32_t heartbeats;
    uint32_t reads_sent;
    uint32_t writes_sent;
    uint32_t writes_dropped;    // queue full
    uint32_t reply_timeouts;
    uint32_t control_timeouts;  // ERV stopped yielding the token
} ha_broan_stats_t;

typedef struct { uint8_t buf[HA_BROAN_MSG_MAX]; uint8_t len; } ha_broan_msg_t;

typedef struct {
    ha_broan_cfg_t cfg;
    bool     inited;

    uint8_t  rxbuf[HA_BROAN_RXBUF];
    size_t   rxlen;

    bool     have_token;
    bool     awaiting_reply;
    uint32_t awaiting_since_ms;
    uint32_t last_token_ms;
    bool     seen_erv;
    uint32_t last_frame_ms;

    bool     want_pong;
    uint8_t  pong[BROAN_MAX_PAYLOAD];
    uint8_t  pong_len;
    bool     want_token_ack;

    ha_broan_msg_t txq[HA_BROAN_TXQ_DEPTH];
    uint8_t  txq_head, txq_count;

    uint32_t last_heartbeat_ms;
    bool     heartbeat_primed;        // false until the first heartbeat lands
    bool     control_timeout_latched; // so a stuck ERV counts once, not once per call

    uint32_t last_poll_ms;
    uint8_t  poll_cursor;          // index into the poll list; 0 = sweep complete
    bool     poll_sweeping;

    ha_broan_field_t fields[HA_BROAN_MAX_FIELDS];
    uint8_t  nfields;

    ha_broan_stats_t stats;
} ha_broan_t;

void ha_broan_init(ha_broan_t *b, const ha_broan_cfg_t *cfg, uint32_t now_ms);

// Feed bytes off the bus. Reassembles frames, answers pings, tracks the token, fills the field cache.
//
// Register responses are harvested REGARDLESS of which controller they were addressed to — so a
// listen-only build parked next to the real wall control collects genuine telemetry before we ever
// take the bus. That is the cheapest possible validation of the whole map.
void ha_broan_rx(ha_broan_t *b, const uint8_t *data, size_t len, uint32_t now_ms);

// Next frame to put on the wire, or 0 if nothing is due. Always returns 0 when listen_only.
size_t ha_broan_next_tx(ha_broan_t *b, uint8_t *out, size_t cap, uint32_t now_ms);

// ── control (queued; transmitted while we hold the token) ─────────────────────
// All return false if the queue is full or the value is refused.
bool ha_broan_set_fan_mode(ha_broan_t *b, uint8_t mode);           // refuses BROAN_FAN_OVR
bool ha_broan_set_manual_cfm(ha_broan_t *b, float supply, float exhaust);
bool ha_broan_report_humidity(ha_broan_t *b, float rh);            // the ERV has no RH sensor — we feed it
bool ha_broan_report_temperature(ha_broan_t *b, float temp);

// ── cached reads ──────────────────────────────────────────────────────────────
bool ha_broan_get_f32(const ha_broan_t *b, uint16_t reg, float *out);
bool ha_broan_get_i32(const ha_broan_t *b, uint16_t reg, int32_t *out);
bool ha_broan_get_u8(const ha_broan_t *b, uint16_t reg, uint8_t *out);
// UINT32_MAX if the register has never been seen.
uint32_t ha_broan_age_ms(const ha_broan_t *b, uint16_t reg, uint32_t now_ms);

// ── health ────────────────────────────────────────────────────────────────────
bool ha_broan_have_token(const ha_broan_t *b);
// True once the ERV has been heard from and is still yielding the token within control_timeout_ms.
bool ha_broan_online(const ha_broan_t *b, uint32_t now_ms);
// Milliseconds since our last heartbeat landed. Compare against control_timeout_ms to see how close the
// unit is to E50. Returns 0 before the first heartbeat (nothing owed yet — we have not taken the bus).
uint32_t ha_broan_e50_exposure_ms(const ha_broan_t *b, uint32_t now_ms);

void ha_broan_get_stats(const ha_broan_t *b, ha_broan_stats_t *out);
