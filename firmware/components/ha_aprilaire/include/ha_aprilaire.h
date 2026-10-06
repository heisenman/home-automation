// BREADCRUMB: firmware/components > ha_aprilaire - Aprilaire E070 "Remote" (Model 76) RS-485 frame codec: parse the unit's M-frames, build our R-frame replies, checksum. Pure + host-tested; the UART is the caller's (ha_rs485). Contract: ADR-0041. Parent: firmware/AGENTS.md.
// REUSE-WHEN: anything that talks to an Aprilaire 1830/1850/1870/E070 on its Remote A/B terminals in place of a
// Model 76. Protocol from dwrice0/aprilaire_controller, verified live on our E070 2026-10-06 (design §2.8).
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// Wire: 9600 8N1, frames STX(0x02) <ASCII body> <2 hex checksum> ETX(0x03).
// Checksum = (0x02 + every body byte) mod 256, as two uppercase hex chars.
#define HA_APR_STX 0x02
#define HA_APR_ETX 0x03

// M-frame body (E070 -> remote, ~1 Hz while REMOTE is enabled): 'M' cmd rh[2 hex] code[2 hex]
//   cmd '?' = idle (blower may be sampling), '!' = dehumidifying.
//   code = the unit's error code: 0 = none, 3 = E3 "Model 76 communication loss", etc. (Model 76 manual Table 2).
typedef struct {
    bool    running;   // cmd '!'
    uint8_t rh_pct;    // unit's own RH sensor (incoming-air RH)
    uint8_t code;      // 0 = OK, n = En on the unit's display
} ha_apr_m_t;

// R-frame (remote -> E070, one per M-frame): 'R' on[2] dryness[2] rh_x10[4] temp_flag[2] temp[2]
//   dryness 1..7 = incoming-air dew-point setpoint 65 °F .. 40 °F (Model 76 manual p.3). The unit runs its
//   compressor when it is ON and the incoming air's dew point is above the setpoint (after a 3-min sample).
//   Raising dryness triggers an immediate sample (manual p.19). rh_x10/temp are the remote's own readings
//   (shown on a Model 76 display; not used for the run decision); temp_flag 0x01 = temp not valid.
typedef struct {
    bool     on;
    uint8_t  dryness;   // 1..7
    uint16_t rh_x10;    // 0..1000
} ha_apr_r_t;

uint8_t ha_apr_checksum(const uint8_t *body, size_t len);

// Parse a body (the bytes strictly between STX and ETX, checksum included). Returns true for a valid,
// checksummed M-frame and fills *out; false for anything else (bad checksum, not 'M', malformed).
bool ha_apr_parse_m(const uint8_t *body, size_t len, ha_apr_m_t *out);

// Build a complete R-frame (STX .. ETX) into out. Returns its length, or 0 if cap is too small or the
// values are out of range. Temperature is always sent as "not valid" (we have no remote temp to offer).
size_t ha_apr_build_r(const ha_apr_r_t *r, uint8_t *out, size_t cap);

// Incremental STX..ETX framer for a byte stream: feed bytes; returns true when a body is complete
// (body/len valid until the next feed). Bodies longer than HA_APR_MAX_BODY are dropped.
#define HA_APR_MAX_BODY 32
typedef struct { uint8_t body[HA_APR_MAX_BODY]; size_t len; bool in; } ha_apr_framer_t;
bool ha_apr_feed(ha_apr_framer_t *f, uint8_t c);
