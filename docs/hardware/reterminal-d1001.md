# reTerminal D1001 — hardware reference (main board V1.0)

**Single source of truth for D1001 board hardware.** Grounded in the official Seeed KiCad
schematic (`reTerminal D1001 main board V1.0 SCH & PCB_251128`, provided by Hugh at
`~/Desktop/Profile/home_automation/docs/D1001_Docs/`) + the `Seeed-Studio/reTerminal-D1001` BSP
(GitHub). **Rule ([[feedback-docs-first]]): when a hardware fact is in doubt, read these docs — do
not reverse-engineer from I2C scans or LLM overviews.** Guessing produced multiple wrong facts
(see "Corrections" below) before the schematic was consulted.

> Schematic is not yet archived in-repo (large KiCad+PCB zip). TODO: commit a copy or a PDF export
> under `docs/hardware/reterminal-d1001-schematic/` so this reference is self-contained.

## SoCs / radios
- **ESP32-P4** — host application MCU (display, app, SD, USB). No native wireless.
- **ESP32-C6FH4** — WiFi/BLE radio, attached over SDIO as an **esp-hosted** co-processor (stock
  esp-hosted firmware; do NOT reconcile it to edge app firmware — the reconciliation surface is the
  P4 beachhead app which shares `ha_ble_scan`).

## Power architecture (sheets `PowerManagement`, `03 Power`, `PowerTreeDiagram`)
**Charger = TI BQ25616 (U16), run in STANDALONE / hardware mode.**
- Integrated **power-path** (VBUS → PMID → SYS/BAT): this is the "DPPM" behaviour — the system rail
  is fed from the input with priority; the battery gets charged from whatever input headroom remains.
- Pins on the sheet: `VBUS, PMID, BTST, SW, SYS, BAT, ILIM, ICHG, TS, STAT, CHG_ENBn, GND`.
  **There is NO SCL/SDA on the charger** — confirmed by grep of the whole PowerManagement sheet
  (zero i2c/scl/sda tokens). So:
  - **Charge current = `ICHG` pin resistor = 680 Ω → 995 mA target** (schematic note), **Vset (CV)
    = 4.2 V**. Fixed in hardware; NOT firmware-settable. ⇒ a real ~1 A charge is *designed*, not a
    trickle — the board is meant to recharge the cell meaningfully (~2.5–3 h from empty at 995 mA).
  - **Input current limit = `ILIM` pin resistor = 330 Ω → ~1.45 A ceiling** (schematic note:
    `I = 478/R = 1448 mA`). This is a *healthy* ceiling and NOT the bottleneck — see below.
  - **Type-C: NO PD/port controller; CC1/CC2 = plain 5.1 kΩ Rd pulldowns** ⇒ dumb sink, 5 V only, no
    voltage negotiation, no active current advertisement read. `usb_mv` ≈ 4.8 V confirms 5 V.
  - **⚠️ CORRECTION (2026-07-02):** an earlier version of this doc concluded the board "can't charge
    while running" due to a low unraisable IINDPM. **That is WRONG — falsified by production reality:**
    the D1001 ships and charges for everyone, so the hardware charges fine while running on the 1.45 A
    ILIM (BC1.2/I2C are conveniences, not requirements for a standalone ILIM-resistor charger). The
    error was *guessing* the IINDPM POR default instead of trusting the "it obviously works in
    production" sanity check (Hugh). **The real variable on our unit is our CUSTOM firmware**, which is
    the only difference from a working stock D1001. `EN_BAT_CHGn` has a 100k pull-down ⇒ hardware
    default = charge-ENABLED; a stock unit that never drives `EXP_GPO10` just charges. **CONFIRMED by
    reading the factory BSP** (`Seeed-Studio/reTerminal-D1001`, `esp32_p4_re_terminal_d1001.c`
    `bsp_battery_charge_task`): the factory (a) does NOT drive the enable at boot — `bat_chg_state`
    inits `true` and it acts only on state-change, so the 100k pull-down holds charge enabled; (b) uses
    simple voltage hysteresis only (disable >4150 mV, enable <3800 mV, enable on USB-insert); (c) has
    thermal protection COMPILED OUT (`BSP_BATTERY_CHARGE_PROTECT 0`); (d) NEVER pulses the enable; (e)
    never steers the BQ (just enable/disable → BQ autonomously CC/CVs at 995 mA/4.2 V). **Our
    `ha_battery` diverges in 3 ways, each able to suppress charge:** (1) `charge_set(false)` at startup
    (disables); (2) thermal-gate on IMU temp (factory OFF — fail-closed if temp read fails); (3) **a
    watchdog that PULSES the enable off→on every 15 s forever** (STAT never reads charging → perpetual
    pulsing → each re-enable restarts the BQ charge cycle → never establishes → self-inflicted). **FIX:
    mirror the factory — drop the watchdog pulse, don't disable at startup, drop/fail-open the thermal
    gate; keep simple hysteresis. The hardware charges fine (production proof); we broke it.**
  - **⭐ ROOT CAUSE of no-charge-while-running: the charger is BLIND to the source.** USB-C `D+/D-`
    route to the P4's **native USB PHY — DP=GPIO25 (pin53), DN=GPIO24 (pin52)** (nets `USB_JT_DP/DN`;
    the same USB we flash over), **NOT to the BQ25616's D+/D-**. So the
    charger cannot run BC1.2 detection to negotiate high input current from a capable charger — it
    runs only on the ILIM ceiling + its POR default `IINDPM`. With no I2C to raise `IINDPM` either,
    the input budget is fixed and modest, and the running P4+C6+display draw on the shared power-path
    input consumes it → ~0 left for the 995 mA charge stage → cell holds / slightly discharges on
    wall. **Three independent reasons it can't charge while running, all hardware, none firmware-
    fixable:** (1) no I2C to the charger, (2) BC1.2 bypassed (D+/D- → P4), (3) shared power-path input
    budget eaten by system load. Charging resumes only when system load drops below the input budget
    (powered off / deep-idle). This is a Seeed design tradeoff: USB *data* to the P4 was prioritized
    over BC1.2 *charge negotiation*.
  - **Enable = `CHG_ENBn`** (active-low) driven by **PCA9535 expander pin 10** (our firmware asserts
    this — it was the root-cause fix for "63% at full charge": the cell never topped up until we
    drove CHG_ENBn low).
  - **Status = `STAT`** pin (1-bit: charging / not) on **GPIO15** (active-low) + **`VSYS_PG`** on
    GPIO4. This is the ONLY charge telemetry available to firmware.
  - **`TS`** = battery NTC (thermistor); the BQ handles thermal charge gating in hardware
    (our firmware's thermal gate via the IMU is belt-and-suspenders on top of this).
- **Autonomous behaviour that explains "won't charge while running" (BQ25616 datasheet):**
  - **VINDPM** (input voltage dynamic power management): if VBUS sags under load below the VINDPM
    threshold, the charger *reduces charge current toward zero* to keep the system alive. A weak/
    sagging supply → charge current collapses. (This is the mechanism behind the brownout wedge and
    the no-charge-at-65% plateau.)
  - **IINDPM / ILIM**: input current is capped at the resistor-set ILIM **and** BC1.2 source
    detection. If ILIM ≈ the running system draw, the battery gets ~0 net current regardless of how
    much the wall brick *could* supply — the board never asks for more.
  - Net: on wall power the cell sits at an **equilibrium** (observed ~65% / 3.85 V), neither charging
    nor meaningfully discharging, until system load drops below the input budget (i.e., the SoC is
    powered off / deep-idle). **This is by hardware design, not a firmware bug and not fixable in
    firmware.**
- Rails: **TPS631000DRLR** (buck-boost) ×2, **SY7200AABC** ×2, **SGM2040-3.3 / SGM2036S-2.8 /
  SGM2036S-1.8** LDOs, **RT9043GB** LDO, **SY6280AAC** ×4 current-limited load switches (rail gating).

### ⚠️ What firmware CANNOT do (settled)
- **Cannot read input current or battery current** — there is **no INA / no current-sense IC / no
  fuel gauge** anywhere on the board. Only *voltages* are sensed (ADC dividers, below).
- **Cannot change the input current limit or charge current** — both are `ILIM`/`ICHG` resistors.
- **Cannot read charge status beyond 1 bit** — the BQ is not on any bus; only the `STAT`/`PG` GPIOs.
- ⇒ **Measuring real input current requires an external inline USB-C power meter.** There is no
  in-firmware path. (This closes the "poll/publish current over MQTT" request: infeasible on this HW.)

### Battery
- Single-cell LiPo. Capacity per a web/AI source was "~2500 mAh" — **UNVERIFIED**; not found in
  schematic. Confirm from the cell label / Seeed spec before quoting.
- Role = **DPPM ride-through UPS buffer**, not a cyclic power source. Proven: it carried the panel
  through a live USB→wall-charger swap with zero reset. It recharges meaningfully only when SoC load
  drops below the input budget (powered off / deep-idle on charger).

## Battery / rail sensing (ADC — our `ha_battery` component)
- **Battery voltage:** net `READ_VBAT` → **P4 GPIO18** = **ADC1 CH2** (schematic-confirmed via the
  PowerManagement hierarchical sheet). ×2 divider, 12-bit / 12 dB / curve-fit cali, sorted-avg.
  Our `adc_ch_batt = 2` is correct. *(A web/AI overview claimed GPIO7 / ADC_CHANNEL_6 — WRONG.)*
- **USB/VSYS rail voltage:** ADC1 **CH1**, ×2 divider. Reads ~4.8 V ⇒ the board runs at 5 V (not a
  high-voltage PD contract).
- **Board temp:** LSM6DS3 IMU internal temp (`raw/256 + 25`).

## I2C inventory (buses per `I2CTreeDiagram`)
Confirmed parts (7-bit addr): **PCA9535 expander @0x20**, **LSM6DS3 IMU @0x6A**, **PCF8563 RTC
@0x51**, **ES7210 audio ADC @0x40/0x41** (A0/A1-selectable — this is the 0x40/0x41 that appears in a
raw bus scan; it is AUDIO, not a current monitor), **GSL3670 touch**, plus config EEPROM.
- Live `cmd/i2cscan` (2026-07-02) saw: `i2c0: 0x36,0x40` · `i2c1: 0x18,0x20,0x25,0x41,0x4B,0x52,0x6A,0x71`.
- **Full address→part reconciliation is still OPEN** (some scan addresses not yet matched to schematic
  refs — 0x18/0x25/0x4B/0x52/0x71/0x36). Resolve against `I2CTreeDiagram` before relying on any of them.

## GPIO / expander map (SCHEMATIC-CONFIRMED — PowerManagement hierarchical sheet)
The PCA9535 @0x20 (U27) exposes `EXP_GPO0..15`; **net `EXP_GPO<n>` = port P(n/8).(n%8) = driver
bit n** (P0.0–P0.7 = bits 0–7, P1.0–P1.7 = bits 8–15). So `EXP_GPO10` = P1.2 = physical pin 12 =
driver `1<<10`. Power-relevant lines:

| Signal (PowerManagement) | Net | Phys | `ha_battery` field | ok |
|---|---|---|---|---|
| `EN_BAT_CHGn` charge enable (active-low, 100k pull-down) | `EXP_GPO10` (P1.2) | pin12 | `exp_charge_en_mask=1<<10` | ✅ |
| `EN_READ_VBAT` sense-divider enable (active-high) | `EXP_GPO6` (P0.6) | — | `exp_read_en_mask=1<<6` | ✅ |
| `CHARGE_STATE` (STAT in, active-low) | — | GPIO15 | `gpio_charge=15` | ✅ |
| `VSYS_PG` power-good | — | GPIO4 | `gpio_vsys_pg=4` | ✅ |
| `READ_VBAT` battery analog | — | GPIO18 (ADC1_CH2) | `adc_ch_batt=2` | ✅ |
| `EN_LCD_PWR` | `EXP_GPO7` | — | (display BSP) | |
| `EN_VDD_CAM` | `EXP_GPO1` | — | | |
| `PCIE_PWR_EN` | `EXP_GPO15` | — | | |
| `LCD_PWM` | — | GPIO14 | (display BSP) | |

**⇒ every pin in the `ha_battery` D1001 preset is verified against the schematic.** The charge-enable
was the suspected culprit; it is wired and driven correctly. Charging is limited only by the BQ25616
hardware (ICHG resistor + VINDPM), which firmware cannot change.
- Other P4 GPIOs (firmware): **GPIO3** = back button (active-low), **GPIO46** = SD VDD switch.
- SDMMC: single P4 host — SD on **slot 0**, C6 esp-hosted on **slot 1** (uses named `SDIO_CLK/CMD/D0–D3`
  nets, NOT GPIO10 — no conflict with the charge enable).

## OPEN items (do NOT guess — read the schematic or ask)
1. ✅ RESOLVED (Hugh, from schematic notes): `ICHG`=680 Ω → **995 mA** charge target, **Vset=4.2 V**,
   `ILIM`=330 Ω input ceiling. Still useful: what input-current-limit (A) the 330 Ω ILIM sets, and
   what BC1.2 mode the input detects (USB100/500/DCP) — that's the input budget that determines
   whether it can ever charge while running.
2. **Battery capacity** (label/spec) — "~2500 mAh" is unverified.
3. **Full I2C address map** — reconcile the scan addresses above against `I2CTreeDiagram`.
4. **Decisive test:** powered-fully-off-on-charger ~20–30 min — design predicts the cell climbs at
   up to 995 mA. If it does → healthy, in-service charging is just budget-starved. If flat → fault.

## Corrections this reference supersedes (guessed-wrong facts, now fixed)
- ❌ "Only the IMU + expander are on I2C" → there are ~8–10 devices.
- ❌ "0x40 = INA219 current monitor" (guessed twice) → **ES7210 audio ADC**.
- ❌ "0x40 = GSL3670 touch" (older note) → touch is a different address; 0x40/0x41 = ES7210.
- ❌ "The charger might be I2C-programmable / we can raise the input current limit" → **no, it is
  standalone; ILIM/ICHG are resistors.**
- ✅ "No I2C fuel gauge" — this old note was CORRECT (no MAX17048); 0x36 is some other device, TBD.

## Audio subsystem (roadmap #6 audible alerts — DOCS-FIRST, from `Audio.kicad_sch` + Seeed BSP)
Authoritative sources: `docs/D1001_Docs/.../Audio.kicad_sch` and the Seeed BSP header
`~/reterminal-dev/reTerminal-D1001/components/esp32_p4_re_terminal_d1001/include/esp32_p4_re_terminal_d1001.h`
(+ the proven `driver_examples/01_I2SCodec` reference).

**Signal chain (playback/alerts):** ESP32-P4 I2S → **ES8311** mono codec (DAC) → **NS4150B** Class-D
power amp → speaker. (**ES7210** is a *4-channel mic ADC* — INPUT only, NOT the alert path. This is the
chip earlier mis-called an "INA219 current monitor"; see the corrections list above.)

| element | detail |
|---|---|
| Codec | **ES8311**, I2C addr **0x18** (`ES8311_ADDRESS_0`, CE low) on **I2C_1** (SCL=GPIO21, SDA=GPIO20) |
| Amp | **NS4150B** Class-D mono; **enable = PCA9535 expander pin 11** (`BSP_POWER_AMP_EN = 1<<11`), NOT a P4 GPIO |
| DAC I2S | MCLK=GPIO33, BCLK=GPIO32, WS/LRCK=GPIO31, **DOUT=GPIO30** (P4→codec) |
| Mic ADC | ES7210 (4-ch, AMIC1-4) on same I2C_1; I2S MCLK=29/SCLK=28/LRCK=27/SDIN=26 — input path, unused for #6 |

**Key integration constraint (why the BSP example isn't drop-in):** the BSP's `es8311_create(port, addr)`
uses the **legacy** IDF i2c driver (`i2c_driver_install`), but the panel already drives I2C_1 with the
**new** `i2c_master` driver (`bsp_i2c1()` → `i2c_master_bus_handle_t`, shared by ha_rtc/ha_imu/expander).
Legacy + new cannot coexist on one port. So `ha_audio` must init the ES8311 either via a new-driver codec
path (esp_codec_dev / a bus-handle es8311) or via raw register writes on `bsp_i2c1()` (register set in the
BSP `es8311_reg.h`). The I2S TX side (new `i2s_std` driver) is unaffected. PA-enable reuses the existing
`bsp_io_expander()` (PCA9535) the panel already controls — set expander pin 11 high before playback.
