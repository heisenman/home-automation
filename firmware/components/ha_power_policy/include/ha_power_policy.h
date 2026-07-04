// BREADCRUMB: firmware/components > ha_power_policy - board-agnostic battery SAFETY policy: mV thresholds -> hard-off / warn / boot-gate, via injected actuators. Contract: ADR-0024. Parent: firmware/AGENTS.md.
// REUSE-WHEN: a battery device needs low-power safety behavior that holds even before an accurate SoC curve exists
//
// Battery low-power SAFETY policy (ADR-0024 §3/§4/§6) — the board-agnostic "safety heart".
//
// Decides, from a single normalized cell voltage, when to: hard power-off at the run floor
// (§3), warn in the 5–10% band (§4), and hold the display dark at boot below the cold-start
// floor (§6). It is deliberately **LUT-free** — it acts on millivolt thresholds, not SoC% —
// so the safety policy holds even before an accurate V→SoC profile exists (ha_battery_profile
// refines the *gauge*; this module keeps the device *safe* regardless).
//
// Split by design:
//   * pure decision core (this header's eval/boot_ok/d1001_cfg) — no ESP deps, host-tested;
//   * runtime (boot_gate/monitor) — drives injected actuators (power_off/warn/led/read_mv).
// A new battery device reuses the whole thing: supply a cfg (its measured thresholds) + the
// four board callbacks. Nothing here is D1001-specific except ha_power_policy_d1001_cfg().
#pragma once
#include <stdbool.h>

// Voltage thresholds, all in **base-frame** cell mV (on-battery / display-on / not-charging).
// The caller normalizes its raw reading into this frame before feeding it in (read_mv), so the
// module never has to know the power state.
typedef struct {
    int shutdown_mv;        // <= this for `shutdown_debounce` samples -> hard power-off (run floor)
    int warn_mv;            // <= this (and > shutdown) -> warn "charge me"
    int warn_clear_mv;      // >= this -> clear the warning (hysteresis; must be > warn_mv)
    int boot_gate_mv;       // boot: below this -> hold the display dark (cold-start floor)
    int boot_release_mv;    // boot: >= this -> release the display (hysteresis; >= boot_gate_mv)
    int shutdown_debounce;  // consecutive sub-floor samples required before power-off (>= 1)
} ha_power_policy_cfg_t;

// reTerminal D1001 preset (v1 — bench-characterized 2026-07-03; provisional, see ADR-0024 §5).
// Base frame = on-battery / display-on / not-charging.
ha_power_policy_cfg_t ha_power_policy_d1001_cfg(void);

// ---- Pure decision core (no ESP deps; host-tested) -------------------------------------

typedef enum {
    HA_PP_NONE = 0,   // nothing to do this sample
    HA_PP_WARN_ON,    // just entered the warn band -> raise the warning
    HA_PP_WARN_OFF,   // just recovered above warn_clear_mv -> clear the warning
    HA_PP_SHUTDOWN,   // sub-floor debounced -> hard power-off NOW
} ha_pp_action_t;

// Caller-owned running state; zero-initialize before the first eval.
typedef struct {
    int  low_streak;  // consecutive samples <= shutdown_mv
    bool warned;      // currently in the warn state (drives one-shot WARN_ON/OFF edges)
} ha_pp_state_t;

// Fold one normalized reading into `st` and return the action to take. Deterministic and pure
// so the host unit test can cover every branch. Shutdown takes precedence over warn.
ha_pp_action_t ha_power_policy_eval(const ha_power_policy_cfg_t *cfg,
                                    ha_pp_state_t *st, int norm_mv);

// Boot-gate verdict: true = the cell is high enough to power the display's inrush safely.
// `was_dark` selects the hysteresis edge: while still dark, require boot_release_mv (climb-out);
// once lit, only boot_gate_mv keeps it lit. (The cold-start floor sits *above* the run floor —
// inrush needs more headroom than steady draw.)
bool ha_power_policy_boot_ok(const ha_power_policy_cfg_t *cfg, int norm_mv, bool was_dark);

// ---- Runtime (injected actuators; ESP side) --------------------------------------------

typedef struct {
    int  (*read_mv)(void *ctx);        // current normalized cell mV (base frame)
    void (*power_off)(void *ctx);      // hard rail-latch release (never returns on battery)
    void (*warn)(void *ctx, bool on);  // UI banner + out-of-band notify ("charge me")
    void (*led)(void *ctx, bool on);   // low-power status LED (boot-gate blink)
    void *ctx;                         // opaque, passed to every callback
} ha_power_policy_io_t;

// Early-boot gate. If the cell is below the cold-start floor, blink `led` and poll until it
// recovers past boot_release_mv, then invoke on_safe(ctx). If already safe, invokes on_safe
// immediately (on the caller's context). Spawns its own task for the low-battery wait so
// app_main is NEVER blocked — the WiFi/MQTT lifeline stays live while the panel is dark.
// on_safe is the "bring the display up" action (D1001: a display_task trigger).
void ha_power_policy_boot_gate(const ha_power_policy_cfg_t *cfg,
                               const ha_power_policy_io_t *io,
                               void (*on_safe)(void *ctx));

// Periodic safety monitor: samples read_mv every period_ms and drives warn()/power_off() via
// ha_power_policy_eval. Spawns one task. Call once, after the gauge is initialized.
void ha_power_policy_monitor_start(const ha_power_policy_cfg_t *cfg,
                                   const ha_power_policy_io_t *io, int period_ms);
