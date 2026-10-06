// aprilaire_bus — the node's side of the Aprilaire E070 Remote (Model 76) RS-485 link (ADR-0041, design §2.8).
//
// From boot it answers EVERY valid M-frame with an R-frame (codec: firmware/components/ha_aprilaire). It only
// ever transmits in reply to the unit, so with the E070 in EXTERNAL mode (bus silent) it never drives the bus.
//
// Control is the same `call` the DH relay carries — the node drives both, so the E070 obeys whichever mode its
// installer menu is in, with no software change to switch:
//   call    -> R on=1, dryness 7 (40 °F dew point: run whenever the incoming air is above it)
//   no call -> R on=0, dryness 1
// The 1 -> 7 dryness step on a new call makes the unit sample immediately (Model 76 manual p.19).
// If this node stops answering, the unit shows E3 and stays off — the same fail-safe as an open relay.
#pragma once
#include <stdbool.h>
#include <stdint.h>

typedef struct {
    bool     link;        // a valid M-frame within the last 5 s (i.e. REMOTE mode and wired)
    bool     running;     // unit reports '!' (compressor dehumidifying)
    uint8_t  rh_pct;      // unit's own incoming-air RH
    uint8_t  code;        // unit error code, 0 = none (n = "En" on its display)
    uint32_t m_ok, m_bad, r_sent;
} apr_bus_status_t;

// Open UART1 (D10/GPIO18 TX, D9/GPIO20 RX, 9600 8N1) and start the bus task. `log` = ha_mqtt_log.
bool apr_bus_start(void (*log)(const char *fmt, ...));

// The call to relay to the unit (mirror of the DH relay level). Takes effect on the next reply.
void apr_bus_set_call(bool on);

// FORCE-RUN (operator Boost): while a call is active, answer on=0x02 instead of 0x01 — the unit then runs its
// compressor regardless of the dryness/dew-point setpoint (observed 2026-10-06, ≈ Model 76 Test Mode).
void apr_bus_set_force(bool force);

void apr_bus_get_status(apr_bus_status_t *out);

// Log counters + a verdict + the last few raw frames (also every 5 min on its own).
void apr_bus_report(void);

// BENCH ONLY: stream `byte` for `secs` (1..30) to prove the TX path. Refused while the link is live.
const char *apr_bus_txtest(int secs, int byte);

// EXPERIMENT: for `secs` (1..900) reply with these RAW R-frame bytes instead of the call mapping — `on` and
// `dryness` may be undocumented values (probing e.g. on=0x02 for Model 76 Test Mode). The DH relay is NOT
// touched, so this also isolates the relay path. secs=0 cancels. Returns a status string.
const char *apr_bus_force(int secs, int on, int dryness);

// Stop transmitting for `secs` (1..3600): frames are still parsed and reported, nothing is sent, so the unit
// sees no remote (E3 after a few seconds, unit off). secs=0 resumes now. Expires on its own. Returns a status.
const char *apr_bus_mute(int secs);
