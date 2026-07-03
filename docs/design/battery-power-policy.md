# D1001 battery power policy — build spec (implements ADR-0024)

**Date:** 2026-07-03  **Status:** Plan (ready to build)  **Owner:** ops.
Reference implementation of the [ADR-0024 battery monitoring & low-power handling standard](../adr/ADR-0024-battery-monitoring-standard.md).
Firmware lives in the off-git dev tree `~/reterminal-dev/d1001-beachhead/`; shared `ha_battery` in
`firmware/components/ha_battery/`. Build in the dev tree, verify on HW @ `.8`, then sync the mirror.

## Measured facts (2026-07-03 characterization session)

Full charge/discharge cycle captured over MQTT (`d1001-beachhead/battprofile`, 15 s cadence) — see the raw
log. All confirmed against Hugh's bench meter and the panel.

**Hardware / firmware handles**
| Function | Handle | Notes |
|---|---|---|
| Cell ADC | `ADC_UNIT_1` ch2 ×2 divider, curve-fit cali | USB sense = ch1 ×2. Divider ×2 matches Seeed. |
| Cell sense enable | expander **P6** (`EXP_BAT_READ_EN`) | active-high |
| Charge /CE | expander **P12** (`EXP_BAT_CHARGE_EN`) | active-LOW (0 = charge enabled). `cmd/charge auto\|hold\|on\|off`. |
| Charge STAT | **GPIO15** (`chg` field) | active-LOW (0 = charging). `cmd/gpio 15` reads it. Correct/trustworthy. |
| Hard power-off | expander **P8** (`PWR_HOLD`) | drop → rail collapses. `bsp_power_off()`. |
| Status LED (red) | **GPIO22** (`BSP_LED_R`) | direct P4 GPIO, active-low (per Seeed). NOT yet wired in beachhead. |

**State offsets** (bring `v_meas` → base frame = on-battery / display-on / not-charging):
- Display **on** vs off: **~+40 mV** (display-on sags the reading; add it back to normalize an off reading, or
  treat display-on as the base).
- **USB** attached: **~+128 mV** step (cell I×R signature under load, ~0.4 Ω internal @ few-hundred mA).
- **Charging**: **~+80 mV** elevation (charger pushes terminal above OCV).

**Anchors** (base frame, `v_norm`):
- **0% → ~3450 mV** — conservative floor, ~100 mV below the tested safe-stop (3504 mV), top of the knee, ~5–8%
  true reserve. (Hugh: "not much below 3500"; safety trip, so the safer end.)
- **~99% → ~3852 mV** — provisional top (observed; charge stalled here with display-on eating the ILIM-capped
  USB input, so true CV termination wasn't reached — refine by re-charging display-off and capturing the STAT
  `charging→done` edge).
- Bottom extrapolated from the measured display-on discharge; **not** below ~3400.

**Power topology (schematic-confirmed, PowerManagement sheet, U16):** the cell reaches the system **only through
the BQ25616's internal BATFET** — `J7 (VBAT) → BQ25616 BAT1/2 → [NVDC BATFET] → SYS1/2 → VSYS`. No discrete
diode/FET path from the cell to VSYS. With USB in, VSYS is bucked from VBUS and the cell charges through the
BATFET; with USB out, the BATFET ties VBAT→VSYS (battery-only). VSYS → TPS631000 buck-boost → VDD_3V3, gated by
`EN_VDD_3V3` = the `PWR_HOLD` latch — so `bsp_power_off()` kills VDD_3V3 (the load), not VSYS, which is why the
rails read 0 V and the cell is preserved.

**USB-kill (forced discharge with USB attached) = NOT firmware-reachable on the D1001 (resolved 2026-07-03).**
Source selection (VBUS vs cell) is internal to U16; its only input-disable (HIZ) is I²C-only and the charger is
not on I²C. The VBUS input leg has no series load switch (the one SY6280 load switch, U11, gates *SD* power via
`SD_PWR_EN`, not VBUS). ACDRV/VAC could drive an external input FET, but it'd be BQ-commanded (I²C), so still
not GPIO-reachable. **Conclusion:** forced discharge needs human intervention (unplug, or a VBUS-leg jumper/cut);
a scriptable kill would require a board mod (a load switch in the VBUS leg with EN on a spare expander line).

**Known gaps:** true CV-termination voltage (charge starved by display load — recharge display-off to get it);
the cold-start/inrush floor (needs a controlled low-battery boot test) → boot gate stays conservative until then.

## Build tasks

**Module map (module-first, ADR-0020 / ADR-0024):** A → extend shared `ha_battery`; E → new
`ha_battery_profile` (loadable curve); **B+C+D → new shared `ha_power_policy`** component (board-agnostic,
actuators injected as callbacks); the red-LED primitive + boot-gate glue are D1001-specific (BSP + `app_main`).
The `ha_power_policy` decisions are reused unchanged on the next device — only its config + callbacks change.

### A. Gauge — state-normalized V→SoC (`ha_battery`)
1. **ADC race fix — DONE** (`batt_adc_init` serialized + local handle + publish-on-success). Keep.
2. **Kill the ratchet-down-only smoother.** Replace with a symmetric filter (or short median) that does not
   latch a transient load sag. This is what pinned 32%→25% on the display step.
3. **Normalize before the LUT:** `v_norm = batt_mv − offset(state)`, `state` from `display_on` (`bsp_display_is_on`)
   + `on_charger` (`usb_mv > present`) + `charging` (STAT). Offsets from the table above (config, not hard-coded).
4. **Rebuild `D1001_LUT`** from the measured discharge curve, anchored 0% = 3450 mV, ~99% = top; monotonic,
   5%-spaced. Validate SoC vs meter at a few points.

### B. Shutdown at 0% (`ha_battery` → `bsp_power_off`)
- When `SoC ≤ 0`, call `bsp_power_off()` immediately (rail latch release — validated today). Debounce lightly
  (e.g. 2 consecutive samples) so a single noisy reading can't trip it, but keep it fast.

### C. Warn at 5–10%
- On `SoC` entering 5–10%: raise a UI warning (panel banner) and publish an MQTT alert (→ ntfy per
  [[air-gap-notify-decision]]). One-shot per crossing (hysteresis), not spammy.

### D. Low-battery boot gate (`app_main` + a small LED driver)
1. Add a minimal red-LED driver (GPIO22, active-low; simple GPIO or LEDC) — new to beachhead.
2. In `app_main`, after `bsp_display_predark()` + `ha_battery_init()` and **before** `bsp_display_start()`:
   read `batt_mv`. If below the **cold-start floor** (provisional ~3500–3550 mV until characterized), **skip the
   display bring-up**, blink red = "CHARGE THE DAMN BATTERY," and poll the cell; bring the display up only once
   it recovers past the floor.
3. Non-fatal and lifeline-safe: WiFi/MQTT still come up so the device stays reachable while dark.

### E. Profile as loadable, versioned data (per ADR-0024 §5, §"re-characterization")
- The LUT + offsets + anchors + floors are a **profile** loaded at boot from NVS or `/sdcard` (fallback to a
  baked-in default), tagged `{version, date, method}`. **Today's constants are `v1` (bench, 2026-07-03) —
  explicitly improvable, not frozen.**
- Accept a pushed profile (MQTT `cmd/battprofile` or an SD file) and hot-swap it without a reflash; keep the
  prior as rollback.
- **Automated re-characterization harness** (bench-hosted first): a script drives the **scriptable** knobs
  (`cmd/charge` /CE, `cmd/screen` display load, sampling rate) through the ADR-0024 cycle, logs the telemetry at
  250 ms–1 s over the transitions, fits a fresh profile, and pushes it back — so accuracy improves each run.
  The **discharge leg stays manual** (USB unplugged): USB-kill is not firmware-reachable (see Power topology
  above), so the harness pauses for a human "USB out" step rather than scripting the source switch.

## Verification (per task, HW @ .8; monitor over USB — panics = Hugh)
- **A:** SoC tracks the meter across display on/off, USB in/out, charge on/off (offsets cancel); no latch on a
  transient; SoC 64%→ correct after recompute.
- **B:** force a low reading (or lower the floor temporarily) → device hard-powers-off; recovers on button.
- **C:** cross into 5–10% → banner + MQTT alert fire once.
- **D:** boot with a low cell (or a temporarily raised floor) → display stays dark, red LED blinks; charge up →
  display comes on. Confirm no display inrush attempted below the floor.

## Then "everywhere"
`ha_battery` carries the mechanism; the offsets/anchors/floors are `ha_battery_cfg_t` fields. A second battery
device supplies its own measured constants (via the ADR-0024 procedure) and reuses B/C/D unchanged. Sync the
mirror + commit (module-first exemplar) when the D1001 build verifies.
