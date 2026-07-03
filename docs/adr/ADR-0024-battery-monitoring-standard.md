# ADR-0024 — Battery monitoring & low-power handling standard

Status: **Accepted** (Hugh, 2026-07-03 — direction) — core direction locked from the D1001 characterization
session; the numeric thresholds (per-device floors, offsets, warn band) are knobs settled by the
per-device characterization procedure below, and the profile is versioned/re-characterizable (§5). Builds on [ADR-0019 screen-interface-architecture](ADR-0019-screen-interface-architecture.md)
and the shared firmware core [ADR-0020](ADR-0020-shared-edge-panel-firmware-core.md); the reusable
implementation lives in the `ha_battery` component. Module-first per [[feedback-modularize-new-architecture]].

## Context

Battery-backed devices (first: the D1001 reTerminal wall panel) need a trustworthy state-of-charge (SoC)
reading and a safe low-power policy. The naive approach — one voltage→percent lookup table (LUT) — is
**wrong**, and the D1001 session (2026-07-03) proved why:

- **Terminal voltage is only a SoC proxy at a fixed load state.** The cell's measured voltage swings with
  load independent of charge. Measured on the D1001: turning the **display on** dropped the reading ~40 mV;
  plugging **USB** in jumped it ~128 mV; **charging** elevates it ~80 mV. A single LUT fed raw voltage
  therefore mis-reads SoC by 2–3 "steps" purely from a power-state change.
- **OCV ≠ in-circuit voltage.** Rested open-circuit voltage of a full Li-ion is ~4.2 V, but under load the
  terminal sags below that, and while charging it sits above OCV. The ADC reads the in-circuit value.
- **Naive smoothing latches.** The D1001's original "ratchet-down-only" smoother permanently pinned SoC to a
  transient load-sag (32%→25% on a display-on step, never recovered).
- **Over-discharge is dangerous** to both the cell (damage) and the system (brownout, and a **cold-start
  current floor we cannot yet measure** — below some voltage the cell can't source display inrush to boot).

We need a standard every battery device follows, not per-device tribal knowledge.

## Decision

Every battery-backed device implements a **state-normalized gauge** plus a four-part low-power policy, and is
onboarded via a **characterization procedure**. The reusable mechanism lives in `ha_battery`; per-device
constants are config.

### 1. State-normalized gauge (not raw voltage → %)

- Read the cell via ADC (per-device unit/channel/divider) with curve-fit calibration. **ADC init MUST be
  race-safe** (serialize under a mutex, claim into a local handle, publish only on success) — the P4 is
  multi-core and the sampler/init/charge-manager all touch it (D1001 bug: a double `adc_oneshot_new_unit`
  NULLed the shared handle → dead gauge).
- **Normalize the reading to a single base frame before the LUT.** Base frame = *on battery, display on,
  not charging* (the state in which the gauge is actually read). Every other state applies a measured
  **offset** to bring it into the base frame: `v_norm = v_meas − offset(state)`.
- Map `v_norm → SoC` through a per-device LUT anchored per §"characterization".
- **No one-way smoothing.** Use a symmetric filter (or hold-through-transient). Never let a load step latch
  the SoC low.

### 2. Anchors — conservative, safety-first

- **0% is a *safety floor above the electrical knee*, not "empty."** It is a shutdown trigger (see §3), so it
  must sit with reserve above (a) the over-discharge damage point and (b) the cold-start/brownout floor. Set
  it just below the tested safe-discharge stop — "not much below," keeping ~5–8% true reserve.
- **100% is the charger's own termination voltage** — capture the charge-status (STAT) `charging→done` edge
  and record `v_norm` there. If the device can't reach termination in test (e.g. load starves charge current),
  anchor a provisional top and refine later; never guess a full-OCV number.
- Bottom of the curve may be **extrapolated** from the measured discharge, but only slightly below the safe
  stop.

### 3. Hard shutdown at 0%

- `SoC ≤ 0%` → **immediate, hard power-off** via the device's rail-latch release (D1001: `bsp_power_off()`,
  drop `PWR_HOLD`). Fast, no ceremony. This both stops the over-discharge and avoids an uncontrolled brownout.

### 4. Warn at 5–10%

- `SoC` in the 5–10% band → user-visible warning (UI) + optional out-of-band notify (MQTT/ntfy) to "charge me."

### 5. Profile is versioned, runtime-loadable data — NOT compiled constants

The LUT, state offsets, anchors, and floors are a **profile** the gauge *loads* (NVS, an SD file, or an
MQTT-pushed blob) — never hard-compiled. Each profile carries a `version` + `date` + `method` tag. This is
the enabler for §"re-characterization": a better curve deploys **without a firmware reflash**, is
comparable/rollback-able, and can even be improved in the field. Firmware ships a baked-in *default* profile
as the fallback when none is loaded.

### 6. Low-battery boot gate

- Read the cell **early in boot** — after the I2C expander + ADC are up but **before** the expensive display
  bring-up. (The rails/ADC are available in the pre-dark stage; the display inrush is the load we're gating.)
- If below a **cold-start-safe floor** (which is *higher* than the run/shutdown floor — inrush needs more
  headroom), **do not bring the display up.** Indicate on a **low-power channel** (a status LED blinking, not
  the display — the display is the load being avoided): "CHARGE THE BATTERY." Poll the cell; bring the display
  up only once it has recovered past the floor.

## Module decomposition (module-first, ADR-0020)

Most of this is **shared firmware modules** (reused across every battery device); only board wiring is
device-specific. The reusable modules take their actuators by **dependency injection** (function pointers /
config) so they're board-agnostic.

**Shared (`firmware/components/`, reused everywhere):**
- **`ha_battery`** (existing) — gauge core: race-safe ADC, sampling, charge manager. Extended to compute a
  **state-normalized** SoC from a loaded profile.
- **`ha_battery_profile`** (new; may start as a unit inside `ha_battery`) — the **versioned, loadable profile**
  (LUT + offsets + anchors + floors + provenance); load/save/hot-swap from NVS/SD/MQTT. Owns the "profile as
  data" decision (§5).
- **`ha_power_policy`** (new) — the **decisions**: SoC → {hard-shutdown, warn, boot-gate}. Board-agnostic:
  consumes SoC and calls injected actuators (`power_off_fn`, `warn_fn`, `display_up_fn`, `led_fn`, `read_mv_fn`).
  This is the reusable heart of the policy — a new device supplies config + those callbacks and reuses it whole.

**Device/board-specific (per-device BSP / app):**
- The **actuator primitives** — rail-latch power-off, display sleep/wake, charger /CE, cell read — already in
  the device BSP; passed into `ha_power_policy` as callbacks.
- A small **status-LED primitive** (D1001: red on GPIO22) in the BSP.
- **Boot-gate glue** in `app_main`: calls `ha_power_policy`'s early-boot check with the board's read/actuate fns
  before display bring-up.

**Host-side ops tooling (not firmware):**
- The **automated characterization harness** — a script driving the knobs over MQTT + fitting a profile — lives
  in ops tooling, not on the device (until/unless on-device self-characterization, which would then be a module).

## Per-device characterization procedure

Onboarding a new battery device produces its constants. Steps:

1. **Instrument.** Stream raw telemetry (raw ADC, cali_mv, cell_mv, usb_mv, STAT/charge flag, temp, and the
   current power-state) over MQTT — SD/CSV optional and **presence-gated**, never a hard dependency. 15 s
   cadence is fine for the slow curve; drop to 250 ms–1 s to capture transitions.
2. **Verify the ADC scale** against a bench meter (divider constant, cali). Do this at a *known* load state;
   remember OCV≠loaded.
3. **Measure the state offsets** by stepping each knob one at a time and reading the step: display on/off,
   USB in/out, charge on/off. Record each Δmv. (D1001: +40 / +128 / +80.)
4. **Discharge at fixed load** (e.g. display-on, USB-out), annotating any transition, down to a *safe* stop
   above the knee — never to true empty. This is the curve shape.
5. **Charge to termination**, watching the STAT `charging→done` edge for the true 100% anchor. Free the input
   budget (blank the display) if system load starves the charge current.
6. **Characterize the cold-start floor** (a controlled low-battery boot test) — the one value the D1001
   session could not get; until measured, set the boot gate conservatively above the run floor.
7. **Set anchors + thresholds**, wire gauge→{shutdown, warn, boot-gate}, and verify each against the meter.

## Actuation knobs (for automated/unattended characterization)

Devices SHOULD expose runtime control so characterization can be scripted rather than hand-run at the bench:

- **Charge enable/disable** (charger /CE line). D1001: expander P12, active-low — confirmed live.
- **Display load** (sleep/wake). D1001: `bsp_display_sleep/wake`.
- **Hard power-off** (rail latch). D1001: `PWR_HOLD`.
- **Read STAT / ADC / state** on demand (D1001: `cmd/gpio`, the telemetry stream).
- **USB-input kill** (force battery drain with USB attached) — **per-device TBD**; requires a firmware-reachable
  input disable / load switch. Not assumed to exist; check the schematic per device. **D1001: resolved NO
  (2026-07-03)** — the BQ25616 is an NVDC power-path charger, so source selection is internal; its only input
  disable (HIZ) is I²C-only and the charger isn't on I²C, and the VBUS leg has no external series switch. Forced
  discharge on the D1001 is human-only; a scriptable kill would need a board mod (VBUS-leg load switch on a GPIO).

## Re-characterization & profile evolution

Characterization is **not a one-shot**. The design is built to re-run it and improve accuracy over time:

- **Deploy without reflash.** A refined profile (§5) is pushed as data — MQTT/SD/NVS — versioned, so accuracy
  improves iteratively and any regression rolls back to the prior profile or the baked-in default.
- **Automated harness.** The actuation knobs let the characterization procedure run **scripted/unattended**
  rather than hand-run: a routine steps display/charge (and USB-kill where reachable), logs the telemetry, and
  emits a fitted profile. First bench-hosted (script drives the knobs over MQTT, fits off-device); can graduate
  to on-device self-characterization. Higher sample rates (250 ms–1 s) for the transition/offset captures.
- **In-field refinement (left open, not required).** The device MAY refine its own profile from real cycles —
  e.g. learn the true termination voltage from live STAT `charging→done` edges, adapt offsets from observed
  load steps, tighten the curve from full charge/discharge excursions — and publish a candidate profile for
  review or auto-adopt under guardrails.
- **Provenance.** Every profile records how it was produced (bench/auto/in-field, date, firmware, device) so
  the fleet's accuracy is auditable and a device can be re-characterized after a battery swap or aging.

The spec therefore treats today's D1001 numbers as **v1, improvable** — not a frozen answer.

## Consequences

- One trustworthy gauge + one safe policy across all battery devices; new devices are a config + a
  characterization run, not a redesign.
- 0% deliberately conservative → the device shuts down with reserve and always recovers; the trade is a few
  percent of unused capacity, accepted for safety.
- Requires each device to expose the actuation knobs and the telemetry stream.
- **Open per-device gap:** the cold-start floor must be measured with a controlled low-battery boot test;
  until then boot gates are set conservatively.

## Status of the reference implementation (D1001)

See [docs/design/battery-power-policy.md](../design/battery-power-policy.md) for the D1001 build spec and the
measured constants. Validated today: state-normalized offsets measured, charge/discharge cycle captured, ADC
race fixed, charge actuation + STAT read + power-off all confirmed. To build: the normalized LUT, the
shutdown/warn/boot-gate policy, and the boot LED indicator.
