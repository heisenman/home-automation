// BREADCRUMB: firmware/components > ha_dout - shared dry-contact / relay OUTPUT driver: fail-safe level, short-cycle dwell, hard max-on watchdog, momentary pulses. Pure + host-tested; GPIO is the caller's. Contract: ADR-0041. Parent: firmware/AGENTS.md.
// REUSE-WHEN: a node has to close a contact into an appliance — a relay, an SSR, any on/off output whose
// stuck-ON state is harmful. Serves ha_aprilaire_dehum (call-for-dehumidification) and ha_gaposa (shade
// UP/STOP/DOWN pulses). NOT for the BLE advert relay filter — that is `ha_relay` (ADR-0015), unrelated.
#pragma once
#include <stdbool.h>
#include <stdint.h>

// ADR-0041. This module owns the *decision* about what a contact should be doing; it never touches a GPIO.
// The caller reads ha_dout_level() and applies it. That keeps the whole thing pure and host-testable, and
// lets the same logic sit behind a GPIO, an I2C expander, or a PhotoMOS array without a fork.
//
// THE FAIL-SAFE STATE IS ALWAYS LOGICAL OFF. Wire the load so that OFF is the harmless state — a normally
// open contact. Every path that gives up (uninitialised, faulted, watchdog overrun) lands on OFF, so an
// unpowered or crashed node leaves the appliance not-calling.
//
// ⚠ Pin selection is the caller's job and it matters: several ESP32 GPIOs are strapping pins or drive
// briefly during reset. Land a relay on one of those and the appliance gets a spurious command on every
// boot and every OTA. Pick a non-strapping pin and pull it to the inactive level in hardware.

typedef enum {
    HA_DOUT_OK = 0,     // applied now
    HA_DOUT_DEFERRED,   // accepted, blocked by a dwell interval; applies on a later tick
    HA_DOUT_REJECTED,   // refused — the request itself is invalid (see each call)
    HA_DOUT_FAULTED,    // latched safe after a max_on_ms overrun; needs ha_dout_clear_fault()
} ha_dout_rc_t;

typedef struct {
    bool     active_high;   // true: logical ON drives the pin HIGH. false: ON drives it LOW.
    uint32_t min_on_ms;     // once ON, stay ON at least this long (compressor short-cycle protection)
    uint32_t min_off_ms;    // once OFF, stay OFF at least this long
    uint32_t max_on_ms;     // HARD ceiling on a continuous ON. 0 = uncapped.
} ha_dout_cfg_t;

typedef struct {
    ha_dout_cfg_t cfg;
    bool     inited;
    bool     on;            // current logical state
    bool     faulted;       // latched after a max_on_ms overrun
    bool     pending;       // a dwell-blocked command is waiting
    bool     pending_on;
    bool     pulsing;
    uint32_t changed_ms;    // when `on` last changed
    uint32_t pulse_end_ms;
} ha_dout_t;

// Call once, early. Starts OFF.
void ha_dout_init(ha_dout_t *d, const ha_dout_cfg_t *cfg, uint32_t now_ms);

// Request a level. A command blocked by min_on_ms/min_off_ms is DEFERRED, not dropped — it lands on the
// tick after the dwell expires, so a call for dehumidification is never silently lost. A command equal to
// the current state cancels any pending opposite command. Returns HA_DOUT_FAULTED (and does nothing) while
// latched.
ha_dout_rc_t ha_dout_set(ha_dout_t *d, bool on, uint32_t now_ms);

// Momentary closure of exactly width_ms, then automatic OFF. This is the shade-controller primitive.
// REJECTED if width_ms is 0, below min_on_ms, or above max_on_ms — all three are configuration errors the
// caller should see rather than have silently clamped. Bypasses min_off_ms only in that the *requested*
// width is honoured; the following OFF still arms min_off_ms for the next command.
ha_dout_rc_t ha_dout_pulse(ha_dout_t *d, uint32_t width_ms, uint32_t now_ms);

// Advance time. Applies pulse expiry, deferred commands, and the max_on_ms watchdog. Call it often — but
// see ha_dout_next_deadline_ms() for why you should not rely on it alone for the hard cap.
ha_dout_rc_t ha_dout_tick(ha_dout_t *d, uint32_t now_ms);

bool ha_dout_on(const ha_dout_t *d);      // logical state
bool ha_dout_level(const ha_dout_t *d);   // PHYSICAL pin level, after active_high
bool ha_dout_faulted(const ha_dout_t *d);

// Clears a latched watchdog fault. Deliberately explicit: a max_on_ms overrun means something upstream
// wedged, and silently resuming would hide it. Arms min_off_ms from now.
void ha_dout_clear_fault(ha_dout_t *d, uint32_t now_ms);

// Milliseconds until the next state change this module will make on its own (pulse end, deferred command,
// or watchdog trip). false when nothing is scheduled.
//
// ⚠ HONEST LIMITATION: ha_dout_tick() is a SOFTWARE watchdog. If the loop that calls it stops, nothing
// forces the contact off. Where a stuck-ON is genuinely harmful — the Gaposa panel latches an error state
// if an input is held past 30 s, and a stuck dehumidifier call runs the compressor indefinitely — the
// adapter should use this deadline to arm an esp_timer one-shot that drives the pin to the inactive level
// directly. That is the only way the cap survives a wedged main loop.
bool ha_dout_next_deadline_ms(const ha_dout_t *d, uint32_t now_ms, uint32_t *out_delay_ms);
