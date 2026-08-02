# RESUME — pause an actuator for n minutes/hours/days, and "off" that is actually off (2026-08-02b)

**Commit:** `de0c5b4`. **Design:** ADR-0011 addendum "a pause is a duration, and 'off' has to mean off".
**Deployed:** ha-2 (live, VIP holder) + the air-gap standby's restore path. **Memory:**
[[dehum-graceful-cant-repower]], [[midea-mode-map]], [[keep-ha2-current-not-frozen]].

## Where it started

Hugh wanted to blow the house out with the windows open — outside was better than inside — but the
dehumidifier kept running. The control sensor sat at **RH 44.0** against `on_above: 44`, so the rule
re-asserted ON every tick: `RH 44 >= 44 -> ON`. The only pause available was a hardcoded **"Off 1h"**, and
he needed 5–8 hours.

## What was actually wrong — two things, and the second one mattered more

**1. The duration was never the constraint.** `POST /control/{id}/override` had always accepted an
arbitrary `duration_min`; only the *presets* in `viewmodel.build_controls` were pinned to 60. So this half
was a UI gap, not a capability gap.

**2. Graceful off was half-implemented — and it hid behind a correct-looking log.** The graceful path
(`idle_mode`, ADR-0011 addendum 2026-07-18) switches the appliance to its idle MODE instead of cutting
power — right for compressor care during ordinary rule cycling. But **mode alone leaves the appliance
self-regulating to whatever target it was left at.** The Midea's target was **35 %**, the *floor* of its
range. So "Off" put it in Set mode chasing 35 % RH and it went right on running.

The control log read `override OFF (359m left)` the entire time. **A pause that was real in the log and
invisible at the wall meter.** Shipping only the duration fix would have handed Hugh a pause button that
did not stop the spending — and the log would have agreed it was working.

## What shipped

- **Arbitrary duration.** Server-authored `custom` block on the override control spec: one number + one
  unit (minutes/hours/days) + an action button, posting the **same `duration_min`** the presets do — one
  endpoint, one validation path. `MAX_OVERRIDE_MIN` **24h → 7d**, still finite on purpose (an override is a
  *timeout*; to park a device indefinitely, disable its automation instead).
- **Setpoint parking.** A **manual** override now also parks the setpoint at its inert end and restores it
  when the override ends.
  - **Which end is inert is config, not code**: `setpoint: {park: max|min|<number>}` in
    `instance/control.yaml`. A dehumidifier idles at the TOP of its range; a humidifier or heater at the
    BOTTOM. Hugh's framing: *"that is a real graceful off"* — and *"I'll ask for parking at min"*, which is
    now a config line, not a code change. Omitting `park` keeps the old behaviour.
  - **Only a manual override parks.** Rule/scene/schedule off keeps the plain `idle_mode`. Parking is the
    semantics of *a human saying stop*, not of the loop cycling.
  - **The pre-park setpoint lives in `control.db` (`setpoint_park`)** and is evaluated **every tick**, not
    only on transitions: a restart mid-pause still restores the right target, a failed park self-heals next
    tick, and the row rides the `sync-standby` snapshot. `set_park` refuses to overwrite an existing row —
    re-parking must never save the **parked** value as the thing to restore.

## Deploy state (verify before trusting)

| Where | What | Note |
|---|---|---|
| **ha-2** (VIP holder, live) | all 6 server files + `control.yaml` `park: max` | restarted `ha-controller`/`ha-api`/`ha-api-tls` behind `instance/.maintenance-fit` so the keepalived probe could not flip the VIP; **flag cleared afterwards**. VIP held. |
| **air-gap standby** (`~/ha-airgap-standby`) | `controller.py` + `control_store.py` + `control.yaml` only | its `control.py`/`viewmodel.py`/`app.js` have drifted (~360/68/205 lines) and its git HEAD is **178 commits behind** with local `failover/` edits — blind-copying those would risk breaking it. The **restore path** is what is failover-critical, and that is fully in the two files copied. `ha-ag-controller` is inactive until failover, so nothing needed restarting. |

**Known gap (deliberate, flag it):** on a failover the standby would serve the **old preset-only** override
UI (its `viewmodel.py`/`app.js` are stale) and its `MAX_OVERRIDE_MIN` is still 1440. Degraded UI, *not* a
stranded appliance — the restore path is current. Closing it means reconciling that checkout properly,
which is its own task, not a drive-by.

**Panel:** the D1001 LVGL renderer (`ui_controls.c`) has no numeric-entry widget and caps at
`MAX_PRESET` 4 buttons, so it ignores `custom` and keeps preset-only pauses. `shared-ui-spec.md` now says
`custom` is optional so a renderer skipping it is conforming, not broken.

## Live state at hand-off

A 6 h override was applied at ~19:12Z (expires ~01:13Z) with the setpoint parked at 85 % **by hand**,
before the feature existed — so a `setpoint_park` row was seeded with the original **35.0** to make the
new code hand the target back on expiry. Verified after deploy: `mode: 1` (Set), `target_pct: 85.0`,
`running: false`, `park = 35.0`.

**Not yet verified live:** the restore itself fires at override expiry. Unit-tested
(`test_expired_override_restores_the_saved_setpoint`) and the deployed files are byte-identical to what was
tested, but nobody has watched the live 85 → 35 hand-back. **Check `target_pct` is back to 35 after the
override ends.**

## The lesson worth carrying

**"Off" delegated to a device's own regulator is only as off as its setpoint.** The control layer had
handed the appliance a mode and considered the job done, so it reported OFF with complete confidence while
the compressor ran. When you hand control to an onboard loop, pin **the input that loop regulates
against**, not just its mode — otherwise you have not turned the device off, you have only stopped asking
it to work, and it will keep its own counsel.

Related in kind to [[migrated-device-silent-drop]] and the SGP41 misidentification: the dangerous failures
here keep being the ones where **every surface reads healthy**.
