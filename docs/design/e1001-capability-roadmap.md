# E1001 capability roadmap — what it should do next, and how

**Date:** 2026-07-04  **Status:** Plan (decompose-before-dev; no code until an item is picked up).  **Owner:** ops.
**Method:** [DEVICE-INTAKE.md](../DEVICE-INTAKE.md), the same walk applied to the D1001
([d1001-capability-roadmap.md](d1001-capability-roadmap.md)). Amends [ADR-0019](../adr/ADR-0019-screen-interface-architecture.md).
See also [e1001-roadmap.md](e1001-roadmap.md), [e1001-epaper-renderer.md](e1001-epaper-renderer.md),
and **[e1001-gap-register.md](e1001-gap-register.md)** — the 2026-07-05 finish-the-E1001 audit (implemented-vs-
stored-vs-missing against ADR-0019/0024) + the step-by-step implementation procedure. **Battery profile v1 is now
DONE** (`d910374`, item #2 below): the loadable ADR-0024 profile shipped; the footer is a measured curve, no longer linear.

## Why this exists

Like the D1001, the E1001 runs a fraction of its silicon. Stage-0 survey of the schematic
(`docs/hardware/reTerminal_E1001_V1_2_SCH_251120.pdf`, sheet 06) shows several **on-board peripherals the
ESPHome firmware never brings up.** This is that backlog, decomposed so each item is pick-up-ready.

**Key difference from the D1001:** the E1001 is **ESPHome**, not ESP-IDF — so "reuse" here means **native
ESPHome components** (a `time: pcf85063` platform, `microphone:`, `output:`+`rtttl:`), not the C `firmware/components/`
modules. And it is a **deep-sleep** device — any always-on feature (mic sensing) trades against battery life, so
the power budget gates those.

## E1001 hardware surface (Stage 0, schematic-confirmed)

| Block | Part | Bus / pins | In firmware? |
|---|---|---|---|
| ePaper | UC8179 7.5" mono | SPI | ✅ display (spec-driven renderer) |
| T&RH sensor | **SHT40** @ `0x44` | I2C0 (IO19/20) | ✅ sensor role |
| Charger | **SY6974B** | I2C1 | ✅ battery profiling |
| Battery sense | ADC | IO2/IO21-EN | ✅ battery % |
| **RTC** | **PCF85063T @ `0x51`, CR1220 coin-cell backed** | I2C0 (IO19/20) | ❌ **unused** (SNTP-only clock, no holdover) |
| **PDM mic** | **MSM261D** | IO41 data / IO42 clk / IO38 PWR-EN | ❌ **unused** |
| **Buzzer** | **MLT-8530** passive | IO45 (BUZZER_EN, via Q7) | ❌ **unused** |
| SD card | microSD | SPI (IO7/8/9, CS IO14) | ❌ unused by firmware |
| Buttons | 3 user keys (KEY0/1/2) | IO3/4/5 | ✅ nav / wake |
| Expansion | 1×8 header (I2C0/UART1/ADC/GPIO) | J2 | — |

## Abilities today (conformance review, catalog A–K)

**Exercises:** A (senses — SHT40 → `home/edge/e1001/sht40/adv` + presence event), D (BLE relay — `switchbot_ble`,
built **not field-validated**), E (display — ePaper, spec-driven), F (battery — profiling built; `ha_battery_profile`
v1 pending the fit), G (wall clock — **SNTP only, no RTC holdover**), I (OTA — ESPHome over WiFi).
**Deliberately not:** B (actuator), C (signed directives — not an enrolled node, same posture as the D1001), H
(data-of-record — has an SD card but is a deep-sleep relay), K (cluster box).

---

## The roadmap (prioritized)

Each item: **Ability** · **Decompose** (ESPHome-native reuse vs new glue) · **Conformance** · **Test** · **Deps**.

### SHOULD #1 — RTC holdover clock: bring up the PCF85063T  ▸ ability G
The strongest item, and **higher-value than the D1001's** for two reasons: (a) the E1001 RTC is **coin-cell
backed** → time survives full battery removal, not just reboots; (b) the E1001 **deep-sleeps**, so today every
wake needs a **SNTP round-trip** before it can show the time (wake latency + radio power). An RTC gives **instant
correct time on wake** and SNTP only re-syncs occasionally → faster, cheaper wakes.
- **Decompose:** almost entirely **reuse** — ESPHome native `time: platform: pcf85063` (address `0x51`, I2C0);
  glue = on SNTP sync, `pcf85063.write_time`; on wake, `pcf85063.read_time` before WiFi so the first ePaper
  frame already has the clock. Datasheet grounding: `docs/hardware/PCF85063TP.pdf` (already in tree).
- **Conformance (G):** sync from the LAN time authority (.210, now serving NTP); degrade gracefully. This is
  the E1001 analogue of D1001 roadmap #1 — same ability, different (simpler) vehicle.
- **Test:** wake with WiFi blocked → clock still right from the RTC; pull battery briefly → time survives (coin cell).
- **Deps:** none (.210 serves NTP). Datasheet in hand.

### SHOULD #2 — Battery profile v1  ▸ ability F
Fit the discharge→charge cycle (**pending a complete cycle in Hugh's terminal**) → E1001 `ha_battery_profile` v1.
Already queued; replaces the provisional linear V→% footer with a measured curve.
- **Deps:** a complete discharge→charge capture (the load-level knob is built).

### COULD #3 — Audible alerting via the buzzer  ▸ new surface on ability E
The ePaper is **silent** — a critical alert has no way to grab attention. The MLT-8530 passive buzzer (IO45)
fixes that: a chime on `g_alert_crit`.
- **Decompose:** reuse ESPHome `output: ledc` (PWM the passive buzzer) + `rtttl:` for a tone; glue = fire on a
  critical alert in the fetch/render path (which already parses `/api/v1/alerts`).
- **Deps:** none. Small.

### COULD #4 — Acoustic sensing via the PDM mic  ▸ ability A (new)
The MSM261D PDM mic (IO41/42, PWR-gated IO38) → a **sound-level / noise sensor**, or a sound-triggered wake.
Novel edge capability (a room noise/occupancy signal).
- **Decompose:** reuse ESPHome `microphone: i2s_audio` (PDM) + a sound-level template sensor → publish on the
  edge sensor path.
- **⚠️ Power caveat:** sampling the mic needs **awake time** — on a deep-sleep device this trades directly
  against battery life. Scope it as a *duty-cycled* sample per wake, or gate it on wall power. Measure the cost
  against the P4e power budget before committing.
- **Deps:** the power-budget number (P4e Step 3).

### Loose ends (not new capabilities — bring-up/validation)
- **BLE relay field-validation (D):** `switchbot_ble` is built but never field-validated.
- **SHT40 registry (A):** gated on Hugh — `e1001-sht40` in `instance/devices.yaml` + restart `ha-edge-mapper`.
- **Deploy:** rename/mount/power + the P4e Step-3 power budget.

### COULD #5 — Local SD logging  ▸ ability H (lite)
The E1001 has an unused microSD. It *could* buffer sensor readings across WiFi outages. But the deep-sleep +
dumb-relay model (the dictator owns history) makes this **low-value** — sequence last, if ever.

---

## Sequencing

**First tranche:** **#1 (RTC holdover clock)** — high-value, mostly reuse (ESPHome native), datasheet in hand,
and it *improves deep-sleep wake* — plus **#2 (battery fit)** which is already gated only on the capture.
**#3 (buzzer)** is a quick, self-contained win. **#4 (mic)** is novel but power-gated — needs the budget first.
Each is additive and OTA-able on the proven E1001 loop. The RTC item is the direct E1001 parallel to D1001 #1 —
same ability G, and a clean second worked example of the intake method (ESPHome vehicle instead of a C module).
