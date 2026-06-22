# ADR-0011 — Automation controller (sensor → policy → actuator)

Status: Proposed (2026-06-22). MVP scope agreed; phased build.

## Context

We now have accurate sensors (BLE meters), a first actuator (Midea dehumidifier under local LAN
control), and a control plane (signed commands + ACL + traits + issuer). What's missing is the layer
that *closes the loop*: turn sensor readings into actuator commands, on a schedule, with manual
overrides the user can time-box. This ADR defines that layer.

## Decision

A new offline service **`ha-controller`** on .245. MQTT-driven; emits every command through the
**existing signed/ACL issuer** (automation uses the same authenticated path as a human). Per device it
evaluates a **policy** on each sensor update / tick and resolves a desired state through a strict
**precedence stack** (highest wins):

```
1. SAFETY / interlocks   tank_full|error → force OFF;  compressor min-off timer;  setpoint clamp
2. MANUAL override (TTL)  "off 2h" | "boost 30m" | "hold target 45 until cleared"  (persisted, expiring)
3. SCHEDULE              time windows (e.g. quiet 22:00–07:00 → off/low)
4. CONTROL rule          the closed loop — a PLUGGABLE per-device strategy
5. DEFAULT               safe resting state
```

Layer 1 is both a **top veto** (tank-full beats a manual "on") and a **bottom clamp** (min-off time
prevents compressor short-cycling — a hard requirement, not advisory).

### Control law is per-device config, NOT a framework default
The loop strategy and its sensor source are configured per device:
- `strategy: hysteresis` — bang-bang with `on_above`/`off_below` deadband + `min_on/min_off`; reads
  `source_sensor` (which may be an EXTERNAL trusted meter or the device's own sensor).
- `strategy: setpoint` — set the device's own target and trust its internal loop.
- (future: pid, multi-sensor.)

`dehumidifier_office` uses `hysteresis` + `source_sensor: meter_pro_living_room` **specifically because
its onboard RH is ~9–15% off and uncalibratable** — this is its config, not the default. A future
well-calibrated unit would use `setpoint` + `source_sensor: self`. The device's onboard RH is still
ingested, flagged NON-authoritative (transport `midea-lan`), never drives control.

### Overrides / timeouts
TTL entries issued through the command API, persisted (survive restart), auto-expiring back to lower
layers. Types: timed-off, timed-boost-on, hold-setpoint-until-cleared. This is the user's
"user-initiated timeout" requirement.

### Observability
Every tick's decision is logged with its REASON to a `control_log`
(`ts, device, desired, source_layer, reason`) — e.g. "override off until 15:30", "RH 56>55 → ON",
"held OFF: min-off 2m left". The system is always auditable: *why* is it in this state.

### Offline-first / fail-safe
Fully local. If the `source_sensor` is stale (no update within N min), the rule layer yields and the
device falls to DEFAULT (safe state) rather than acting on stale data.

## MVP scope (build first)

Closed loop for the dehumidifier, architected generically:
1. **Midea LAN transport** + device driver in the issuer (wraps midea-beautiful-air status/set with the
   saved token+key) → device controllable through the control plane, not just the CLI.
2. **control.yaml** `dehumidifier_office` (traits: switchable power, setpoint target, enum fan/mode) + ACL.
3. **ha-controller** with the PURE resolver: safety (min-cycle + interlocks) > manual TTL override >
   (schedule stub) > hysteresis rule > default. Pure functions, unit-tested (like gap_watcher/mesh).
4. **Override API** endpoint (timed off / boost / hold).
5. Ingest the device's onboard RH as a flagged non-authoritative metric.
6. `control_log` + `ha-controller.service`.

Deferred to phase 2: schedule windows with profiles, full function exposure (pump/ion/sleep traits),
multi-device policies, a UI for overrides.

## Consequences

+ Automation rides the existing signed/ACL path (no new trust surface). + Control law is pluggable, so
the dehumidifier's external-sensor quirk doesn't contaminate the design. + Auditable + fail-safe.
− A new long-running service to own (state: overrides, min-cycle timers, control_log).
