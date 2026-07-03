# ha_power_policy — battery low-power safety policy (ADR-0024)

The **board-agnostic safety heart** for battery-backed nodes. From a single normalized cell
voltage it decides when to:

- **Hard power-off** at the run floor (ADR-0024 §3) — debounced, highest priority.
- **Warn** in the low band (§4) — one-shot per crossing, with hysteresis so it can't flap.
- **Hold the display dark at boot** below the cold-start floor (§6) — the inrush floor sits
  *above* the run floor, so a cell that can idle can still be too weak to light the panel.

**LUT-free by design.** It acts on **millivolt thresholds**, not SoC% — so the safety policy is
correct even before an accurate V→SoC curve exists. `ha_battery` / `ha_battery_profile` refine the
*gauge*; this module keeps the device *safe* regardless of gauge accuracy.

## Shape (module-first, ADR-0020)

- **Pure decision core** (`ha_power_policy.c`) — `ha_power_policy_eval()` (warn/shutdown state
  machine) + `ha_power_policy_boot_ok()` (gate verdict) + the D1001 preset. No ESP deps; every
  branch is covered by the host test (`test/run.sh`, plain `cc`).
- **Runtime** (`ha_power_policy_rt.c`) — a boot-gate task (blink the LED + poll until the cell
  recovers, then release the display) and a steady-state monitor task. Both drive **injected
  actuators** — `read_mv` / `power_off` / `warn` / `led` — so nothing here is board-specific.

## Reuse on a new device

Supply `ha_power_policy_cfg_t` (its measured thresholds) and the four `ha_power_policy_io_t`
callbacks. Reuse `eval` / `boot_ok` / the two runtime tasks **whole** — a new battery device is a
config + four callbacks, not a fork. `ha_power_policy_d1001_cfg()` is the reTerminal D1001 preset
(v1, bench 2026-07-03; thresholds are provisional per ADR-0024 §5 and re-settled by the per-device
characterization run).

## Platform support

Any node that can read a cell voltage and release its own power rail. The D1001 wires
`read_mv`→normalized `ha_battery_sample`, `power_off`→`bsp_power_off`, `led`→the GPIO22 red LED,
`warn`→UI banner + MQTT/ntfy.

## Consumed by

- d1001-panel (reTerminal D1001) — first adopter (integration: LED primitive + app_main boot-gate
  hook + monitor start).
