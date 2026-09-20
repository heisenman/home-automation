// ha_dout — dry-contact / relay output decisions (ADR-0041). Pure: no ESP deps, no GPIO.
// See include/ha_dout.h for the contract.
#include "ha_dout.h"

// Wrap-safe time comparisons. esp_timer millisecond counters wrap every ~49.7 days, and an edge node is
// expected to run far longer than that between reboots. Comparing with `>=` on raw values would, once per
// wrap, either fire every deadline at once or stall one forever — so every comparison here goes through
// an unsigned *difference*, which stays correct across the wrap.
static inline bool elapsed(uint32_t now, uint32_t since, uint32_t interval) {
    return (uint32_t)(now - since) >= interval;
}

// true once `now` has reached `deadline` (wrap-safe: signed difference of unsigned counters).
static inline bool reached(uint32_t now, uint32_t deadline) {
    return (int32_t)(now - deadline) >= 0;
}

// Remaining time until `deadline`, saturating at 0.
static inline uint32_t remaining(uint32_t now, uint32_t deadline) {
    return reached(now, deadline) ? 0u : (uint32_t)(deadline - now);
}

// Is the dwell interval guarding a change out of the current state satisfied?
static bool dwell_ok(const ha_dout_t *d, uint32_t now_ms) {
    uint32_t need = d->on ? d->cfg.min_on_ms : d->cfg.min_off_ms;
    return need == 0 || elapsed(now_ms, d->changed_ms, need);
}

static void apply(ha_dout_t *d, bool on, uint32_t now_ms) {
    if (d->on != on) {
        d->on = on;
        d->changed_ms = now_ms;
    }
    if (!on) d->pulsing = false;
}

void ha_dout_init(ha_dout_t *d, const ha_dout_cfg_t *cfg, uint32_t now_ms) {
    if (!d) return;
    d->cfg          = cfg ? *cfg : (ha_dout_cfg_t){ .active_high = true };
    d->inited       = true;
    d->on           = false;
    d->faulted      = false;
    d->pending      = false;
    d->pending_on   = false;
    d->pulsing      = false;
    d->changed_ms   = now_ms;
    d->pulse_end_ms = now_ms;
}

ha_dout_rc_t ha_dout_set(ha_dout_t *d, bool on, uint32_t now_ms) {
    if (!d || !d->inited) return HA_DOUT_REJECTED;
    if (d->faulted)       return HA_DOUT_FAULTED;

    // A level command supersedes an in-flight pulse: the caller wants a state, not a blip.
    d->pulsing = false;

    if (on == d->on) {          // already there — cancel any pending opposite command
        d->pending = false;
        return HA_DOUT_OK;
    }
    if (dwell_ok(d, now_ms)) {
        d->pending = false;
        apply(d, on, now_ms);
        return HA_DOUT_OK;
    }
    d->pending    = true;       // held, not dropped
    d->pending_on = on;
    return HA_DOUT_DEFERRED;
}

ha_dout_rc_t ha_dout_pulse(ha_dout_t *d, uint32_t width_ms, uint32_t now_ms) {
    if (!d || !d->inited) return HA_DOUT_REJECTED;
    if (d->faulted)       return HA_DOUT_FAULTED;

    // Reject rather than clamp. A pulse outside the configured envelope is a programming error, and
    // silently shortening a shade command (or stretching one past the panel's 30 s lockout) would turn it
    // into a field mystery instead of a compile-then-test failure.
    if (width_ms == 0) return HA_DOUT_REJECTED;
    if (width_ms < d->cfg.min_on_ms) return HA_DOUT_REJECTED;
    if (d->cfg.max_on_ms && width_ms > d->cfg.max_on_ms) return HA_DOUT_REJECTED;

    if (!d->on && !dwell_ok(d, now_ms)) return HA_DOUT_REJECTED;  // still in min_off_ms

    d->pending      = false;
    apply(d, true, now_ms);
    d->pulsing      = true;
    d->pulse_end_ms = now_ms + width_ms;
    return HA_DOUT_OK;
}

ha_dout_rc_t ha_dout_tick(ha_dout_t *d, uint32_t now_ms) {
    if (!d || !d->inited) return HA_DOUT_REJECTED;

    if (d->faulted) {
        apply(d, false, now_ms);   // stay safe while latched
        return HA_DOUT_FAULTED;
    }

    // Watchdog first — it outranks everything, including a pulse that somehow outlived its own deadline.
    if (d->on && d->cfg.max_on_ms && elapsed(now_ms, d->changed_ms, d->cfg.max_on_ms)) {
        apply(d, false, now_ms);
        d->pulsing = false;
        d->pending = false;
        d->faulted = true;
        return HA_DOUT_FAULTED;
    }

    if (d->pulsing && d->on && reached(now_ms, d->pulse_end_ms)) {
        apply(d, false, now_ms);   // the requested width IS the dwell; min_on_ms was validated at pulse()
        return HA_DOUT_OK;
    }

    if (d->pending && dwell_ok(d, now_ms)) {
        bool want = d->pending_on;
        d->pending = false;
        apply(d, want, now_ms);
        return HA_DOUT_OK;
    }

    return HA_DOUT_OK;
}

bool ha_dout_on(const ha_dout_t *d) {
    return d && d->inited && d->on;
}

bool ha_dout_level(const ha_dout_t *d) {
    // Uninitialised counts as OFF, so a half-constructed struct still reads out as the safe level.
    bool on = ha_dout_on(d);
    bool active_high = d ? d->cfg.active_high : true;
    return active_high ? on : !on;
}

bool ha_dout_faulted(const ha_dout_t *d) {
    return d && d->faulted;
}

void ha_dout_clear_fault(ha_dout_t *d, uint32_t now_ms) {
    if (!d || !d->inited) return;
    d->faulted    = false;
    d->pending    = false;
    d->pulsing    = false;
    d->on         = false;
    d->changed_ms = now_ms;   // arm min_off_ms from the clear, not from the overrun
}

bool ha_dout_next_deadline_ms(const ha_dout_t *d, uint32_t now_ms, uint32_t *out_delay_ms) {
    if (!d || !d->inited || d->faulted) return false;

    bool     found = false;
    uint32_t best  = 0;

    if (d->on && d->cfg.max_on_ms) {
        uint32_t t = remaining(now_ms, d->changed_ms + d->cfg.max_on_ms);
        best = t; found = true;
    }
    if (d->pulsing && d->on) {
        uint32_t t = remaining(now_ms, d->pulse_end_ms);
        if (!found || t < best) { best = t; found = true; }
    }
    if (d->pending) {
        uint32_t need = d->on ? d->cfg.min_on_ms : d->cfg.min_off_ms;
        uint32_t t    = remaining(now_ms, d->changed_ms + need);
        if (!found || t < best) { best = t; found = true; }
    }

    if (found && out_delay_ms) *out_delay_ms = best;
    return found;
}
