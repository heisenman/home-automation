// BREADCRUMB: firmware/components > ha_gaposa - Gaposa QCTZ36SDU shade-panel command planner: per-channel + BOX-WIDE interlocks, bounded pulses, open-loop travel-time position. Contract: ADR-0041. Parent: firmware/AGENTS.md.
// REUSE-WHEN: driving a Gaposa dry-contact shade panel (QCTZ3SDU / QCTZ36SDU) from relay outputs. The
// panel owns the 434.15 MHz radio; we only close contacts. Pairs with ha_dout for the physical outputs.
#pragma once
#include <stdbool.h>
#include <stdint.h>

// ADR-0041 §3. The QCTZ36SDU is a 6-channel panel: each channel exposes Up / St / Dw against its own com,
// and a MOMENTARY closure issues the command — the motor then runs to its own stored limit.
//
// TWO INTERLOCKS, and the second one is the one that is easy to miss:
//
//  1. Per channel: never assert two of Up/St/Dw at once.
//  2. ⛔ BOX-WIDE: the panel latches an error — all LEDs on, transmission STOPS — if two channels are
//     driven with DIFFERENT commands at the same instant. The manual's own example is CH3 DOWN together
//     with CH4 UP. Same command across many channels is fine and is the efficient path for "all up".
//     A scene like "close everything except the office" is the natural way to hit this, so this module
//     SERIALISES mixed-direction commands into separate time slots rather than trusting the caller.
//
// And a closure held past 30 s trips the same error state. Every pulse here is hard-bounded well under it
// (HA_GAPOSA_MAX_PULSE_MS); pair the outputs with ha_dout so the cap also survives a wedged loop.
//
// ⚠ There is NO FEEDBACK of any kind. The panel has no output contacts, the RF link is one-way, and the
// motor reports nothing. Position is dead reckoning from travel time and it drifts. Three states are
// genuinely absolute — fully open, fully closed, and the motor's stored intermediate position — because
// those are motor-side limits that re-datum on every full run. Everything else is an estimate and this
// module labels it as one (ha_gaposa_conf_t). Never store an estimate as though it were a measurement.

#define HA_GAPOSA_MAX_CH        6u
#define HA_GAPOSA_MAX_PULSE_MS  5000u   // hard ceiling; the panel's own lockout is 30 s
#define HA_GAPOSA_OPEN          1000u   // per-mille
#define HA_GAPOSA_CLOSED           0u

typedef enum {
    HA_GAPOSA_CMD_NONE = 0,
    HA_GAPOSA_CMD_UP,
    HA_GAPOSA_CMD_DOWN,
    HA_GAPOSA_CMD_STOP,
    // Recall the motor's stored intermediate position: hold St for ~3 s. LIKELY, not documented —
    // inferred from the handheld remote's behaviour. Verify on the bench before relying on it (ADR-0041).
    // Batched separately from CMD_STOP so a short stop and a long hold can never overlap on the same wire.
    HA_GAPOSA_CMD_INTERIM,
} ha_gaposa_cmd_t;

// How much to trust ha_gaposa_position().
typedef enum {
    HA_GAPOSA_POS_UNKNOWN = 0,  // never driven to a limit since boot — we genuinely do not know
    HA_GAPOSA_POS_ESTIMATED,    // dead reckoning: stopped mid-travel, or still moving
    HA_GAPOSA_POS_EXACT,        // parked at a motor-side limit after a completed run
} ha_gaposa_conf_t;

typedef struct {
    uint8_t  channels;            // 1..HA_GAPOSA_MAX_CH
    uint32_t pulse_ms;            // command pulse width (0 => default 500 ms)
    uint32_t interim_hold_ms;     // St hold for CMD_INTERIM (0 => default 3000 ms)
    uint32_t gap_ms;              // quiet time between batches of different commands (0 => default 250 ms)
    uint32_t travel_up_ms[HA_GAPOSA_MAX_CH];    // full closed -> open, per channel. 0 disables the model.
    uint32_t travel_down_ms[HA_GAPOSA_MAX_CH];  // full open -> closed (expect it to differ: gravity helps)
} ha_gaposa_cfg_t;

typedef struct {
    // Exactly one command type across the whole box, or NONE while idle//in the guard gap.
    ha_gaposa_cmd_t batch;
    // Per channel: the contact to close right now. Always NONE or == batch.
    ha_gaposa_cmd_t assert_ch[HA_GAPOSA_MAX_CH];
} ha_gaposa_out_t;

typedef struct {
    ha_gaposa_cmd_t pending;      // queued, not yet sent (latest command per channel wins)
    uint32_t        pending_seq;  // FIFO ordering across channels
    ha_gaposa_cmd_t last_sent;    // what we last actually transmitted — the COMMANDED state
    uint16_t        pos;          // per-mille, 0 = closed
    ha_gaposa_conf_t conf;
    bool            moving_up;
    bool            moving;
    uint32_t        move_start_ms;
    uint16_t        move_start_pos;
} ha_gaposa_ch_t;

typedef struct {
    ha_gaposa_cfg_t cfg;
    bool            inited;
    ha_gaposa_ch_t  ch[HA_GAPOSA_MAX_CH];
    ha_gaposa_cmd_t batch;        // command type currently asserted
    uint8_t         batch_mask;   // channels in the current batch
    uint32_t        batch_end_ms;
    uint32_t        gap_until_ms;
    bool            in_gap;
    uint32_t        seq;
} ha_gaposa_t;

// Returns false if the config is unusable (no channels, too many, or a pulse over the hard ceiling).
// Rejects rather than clamps: a pulse outside the envelope is a programming error, and silently
// shortening a shade command turns it into a field mystery.
bool ha_gaposa_init(ha_gaposa_t *g, const ha_gaposa_cfg_t *cfg, uint32_t now_ms);

// Queue a command. A second command for the same channel replaces the first — you want the newest
// intent, not a backlog. Returns false for a bad channel or command.
bool ha_gaposa_command(ha_gaposa_t *g, uint8_t channel, ha_gaposa_cmd_t cmd, uint32_t now_ms);

// Advance the planner. Fills *out with the contacts to close right now. Call it often.
void ha_gaposa_tick(ha_gaposa_t *g, uint32_t now_ms, ha_gaposa_out_t *out);

// Estimated position, per-mille (0 = closed, 1000 = open), with its confidence. Read BOTH: storing the
// position without the confidence is the mistake this API exists to prevent.
uint16_t ha_gaposa_position(const ha_gaposa_t *g, uint8_t channel, ha_gaposa_conf_t *out_conf);

// The last command actually transmitted for this channel — the COMMANDED state, kept distinct from the
// estimated position above.
ha_gaposa_cmd_t ha_gaposa_commanded(const ha_gaposa_t *g, uint8_t channel);

bool ha_gaposa_busy(const ha_gaposa_t *g);        // a batch is asserted or work is queued
bool ha_gaposa_moving(const ha_gaposa_t *g, uint8_t channel);
