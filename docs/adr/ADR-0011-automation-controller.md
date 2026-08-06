# ADR-0011 — Automation controller (sensor → policy → actuator)

Status: Accepted — **IMPLEMENTED & LIVE** (2026-06-22). The full MVP shipped: pure resolver
(`server/control/automation.py`), Midea LAN driver/transport, signed control plane, `ha-controller`
service running autonomous closed-loop control on .245, plus the override/policy/manual API and the PWA.
(Originally "Proposed (2026-06-22), phased build" — corrected in the 2026-06-23 doc reconciliation.)

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

## Addendum — house scenes (Home/Away/Sleep), 2026-06-24 (board `schedules-modes-scenes`)

Whole-house occupancy **scenes** the user flips from the PWA topbar. One global value in `control.db`
(`house_scene`, single row, default `Home`); the controller reads it every tick and folds each device's
matching profile into the effective policy via the pure `automation.apply_scene()`.

- **Per-device profiles** live in the policy as `"scenes": {"<name>": <patch>}`. A patch may force the
  device **off** (`{"off": true}`) and/or **relax/tighten** the hysteresis thresholds
  (`on_above`/`off_below`/`min_on_min`/`min_off_min`). `Home` (and any device with no profile) = base
  policy unchanged, so the feature is **opt-in and changes nothing until configured**.
- **Precedence:** inserted as a new layer **between manual override and schedule** — an explicit human
  override still beats the ambient scene; safety still beats everything. New decision source `scene`.
- **API:** `GET /api/v1/house` (open read — scene + canonical list) for the selector; `POST
  /control/house/scene` (admin) to set it; per-device profiles edit through the existing
  `PUT /control/{id}/policy` (`scenes` key, validated). The view-model surfaces the active scene's effect
  per device so the card shows "Away: relaxed / Sleep: parked".
- **Failover:** `house_scene` rides the existing `sync-standby` snapshot, so the scene survives a dictator
  swap. **Distinct from** the power `mode.py` (Normal/Conserve/Emergency), which is a separate concern.
- This delivers the deferred "schedule windows with profiles / a UI for overrides" item above. Time-of-day
  **auto-scene** switching (a house schedule writing `house_scene`) is a clean future extension — the row
  is already the single write point; deferred to avoid manual-vs-auto pin arbitration with one actuator.

## Addendum — a pause is a duration, and "off" has to mean off, 2026-08-02
Two gaps surfaced together while trying to hold the dehumidifier off for an evening of open windows: the
outside was better than inside, but the control sensor sat at RH 44.0 against `on_above: 44`, so the rule
kept re-asserting ON.

**1. The override is a duration, not a fixed hour.** The API always accepted an arbitrary `duration_min`
— only the *presets* were hardcoded to 60. The UI now offers **one number + one unit (minutes / hours /
days)** alongside the presets, authored server-side in the `controls` spec (`custom`, see
`docs/design/shared-ui-spec.md`) so both renderers get it from one place, and posting the same
`duration_min` the presets do — one endpoint, one validation path. `MAX_OVERRIDE_MIN` moved 24h → **7d**.
The cap stays finite on purpose: an override is a **timeout**, never a permanent mode, so a forgotten
pause self-clears instead of masquerading as a dead actuator. To park a device indefinitely, disable its
automation (`enabled: false`) — that is the honest way to say "this is not being controlled".

**2. Graceful off was only half-implemented.** The graceful path (addendum above / `idle_mode`) switches
the appliance to its idle MODE rather than cutting power — right for compressor care during ordinary rule
cycling. But mode alone leaves the appliance **self-regulating to whatever target it was left at**. The
Midea's target was 35% (the floor of its range), so an "off" override put it in Set mode where it went
right on running. The pause was real in the control log and invisible at the wall meter.

So a **manual override** now also **parks the setpoint at its inert end**, and restores it when the
override ends:
- **Which end is inert is config, not code** — `setpoint: {..., park: max|min|<number>}` in
  `instance/control.yaml`. A dehumidifier idles at the TOP of its range (nothing left to remove); a
  humidifier or heater idles at the BOTTOM. Omitting `park` keeps the previous behaviour, so this is
  opt-in per device and changes nothing for devices that don't declare it.
- **Only a manual override parks.** Rule/scene/schedule-driven off keeps the plain `idle_mode` — parking
  is the semantics of *a human saying stop*, not of the loop cycling.
- **The pre-park setpoint lives in `control.db` (`setpoint_park`)**, not in memory, and is evaluated every
  tick rather than only on transitions. So a controller restart mid-pause still restores the right target,
  a failed park command self-heals on the next tick, and the row rides the `sync-standby` snapshot so a
  dictator swap mid-pause doesn't strand the device at its parked value. `set_park` deliberately will not
  overwrite an existing row — re-parking must never save the *parked* value as the thing to restore.

**Lesson worth keeping:** "off" delegated to a device's own regulator is only as off as its setpoint. A
control layer that reports OFF while the appliance keeps working is worse than one that reports honestly —
the log said `override OFF (359m left)` the whole time. When handing control to an onboard loop, pin the
input that loop regulates against, not just its mode.

## Addendum — actuators may follow a DERIVED metric, in either direction, 2026-08-06

An air purifier should be able to run off any air-quality sensor in the house, the same way the
dehumidifier binds to an external humidity meter. Three things were in the way, and only one of them was
the obvious one.

**1. The control loop and the storage layer are different readers.** `ha-controller` is its own process
whose only input is MQTT: `Controller.readings` is filled exclusively by `on_message`, and the controller
never opened `hot.db` at all. The unified `air_quality` score (ADR-0035) is *derived* — computed in the
read path by the viewmodel fusion and persisted every 60 s by `ha-gas-quality-sampler` — so it appears in
no `/state` payload. Binding a purifier to a gas node would therefore have saved cleanly and then
**silently never actuated**: `_pick_source` would find nothing, and "source produced no reading" is
indistinguishable from "sensor offline", so the resolver would fail safe to the device default forever.

The fix is a narrow seam, not a second fusion implementation. `DERIVED_METRICS` names the metrics that
never reach MQTT; for those, `_pick_source` reads the newest stored point from `hot.db` (read-only,
best-effort — a missing or locked readings store degrades to "no reading" and can never kill a tick).
Crucially it carries the row's **own** timestamp, so `sensor_stale_min` governs derived and live sources
identically: if the sampler stops writing, the source goes stale and the device fails safe exactly as it
would for a dead radio. Re-deriving the score inside the control loop was rejected — it would have
duplicated the auto-reference-picking fusion and let the control view drift from the displayed view.

**2. Direction is not uniform.** PM2.5 and AQI *rise* as air gets worse. `air_quality` is a
**cleanliness score** — 100 is clean — so it rises as air gets *better*. `_level_for` only requires
ascending `max` cutoffs; the direction lives in the **levels** attached to them, so an `air_quality`
ladder descends (`<20 → speed 4 … ≥60 → speed 1`, cutoffs on the ADR-0035 band edges). This is a silent
failure mode: a PM2.5 ladder re-pointed at `air_quality` runs the fan flat-out in pristine air and idles
it in smoke, with nothing in the log looking wrong. So the UI resets cutoffs **and** the speed ladder
together whenever the metric changes, and `test_a_pm25_ladder_pointed_at_air_quality_is_inverted` pins the
hazard in place.

**3. The MQTT subscription set was computed once at startup**, from `source_sensor` only. Two consequences
nobody had hit yet: `fallback_sensors` were never subscribed at all (so a failover chain could not fail
over), and re-pointing a device's source from the PWA did nothing until someone restarted the controller —
the edit saved, the card kept reading "stale", and nothing said why. The controller now subscribes to
`home/+/+/state` and resolves sources per tick from policy; `on_message` already keys by the payload's
`device_id`, so the wildcard costs one dict entry per device.

**Ghost sources.** `purifier_living_room` was found pointing at `levoit_office` — an id retired in the
rename to `purifier_living_room`, with **zero rows ever recorded** — with automation disabled on top. The
root cause is structural: `device_relocate` / `apply_rename_worksheet` rewrite the registry and migrate
history, but `automation_policy.source_sensor` is free-text they never touched. `tools/repair_control_source.py`
audits every policy for a source that has *never* produced its control metric and repairs the unambiguous
case (a self-sourcing device points at itself); anything else it reports for a human to choose rather than
guessing. See also the lesson from the pause addendum: a control layer that looks healthy while doing
nothing is worse than one that fails loudly.
