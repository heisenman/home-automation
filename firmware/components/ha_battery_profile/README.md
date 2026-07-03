# ha_battery_profile — versioned, loadable battery profile (ADR-0024 §5)

The gauge's numbers are **data, not compiled constants**. State offsets, the V→SoC LUT, and the
safety floors travel together as a *profile* with provenance (`version` / `date` / `method`), so a
better curve deploys **without a firmware reflash**, is comparable / rollback-able, and can be
improved in the field.

## Shape (module-first, ADR-0020)

- **Pure core** (`ha_battery_profile.c`) — the `ha_batt_profile_t` struct plus the two math
  primitives the gauge needs:
  - `ha_batt_profile_normalize()` — raw ADC mV → **base-frame** mV (removes the display-off / USB /
    charging offsets; base frame = on-battery / display-on / not-charging).
  - `ha_batt_profile_soc()` — base-frame mV → SoC%% via linear LUT interpolation (clamped 0..100).
  - `ha_batt_profile_d1001_default()` — the baked-in fallback profile.
  No ESP deps; every branch is covered by the host test (`test/run.sh`, plain `cc`).
- **Runtime** (`ha_battery_profile_rt.c`, added at gauge-integration) — load/save/hot-swap from
  NVS, an SD file, or an MQTT-pushed blob; returns a validated `ha_batt_profile_t`.

## D1001 default (v1)

Bench-characterized 2026-07-03. Offsets measured (+40 display-off / +128 USB / +80 charging). The
LUT is **linear-provisional** between the two measured anchors (0% = 3450 mV, 100% ≈ 3860 mV) —
a placeholder until the regressed curve from a finer-cadence discharge run replaces it (as data, no
reflash). Floors mirror `ha_power_policy_d1001_cfg()`; the caller builds the policy cfg from these.

## Relationship to the other battery modules

- `ha_battery` (gauge) loads a profile and calls `normalize()` → `soc()` per sample.
- `ha_power_policy` (safety) takes its thresholds from this profile's floor fields.
- The characterization harness (ops tooling) *emits* a fresh profile that gets pushed back here.

## Consumed by

- d1001-panel (reTerminal D1001) — first adopter.
