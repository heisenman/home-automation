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
  - **Input current limit = `ILIM` pin resistor** (fixed in hardware; NOT firmware-settable).
  - **Charge current = `ICHG` pin resistor** (fixed in hardware; NOT firmware-settable).
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
- **Battery voltage:** ADC1 **CH2**, ×2 divider, 12-bit / 12 dB / curve-fit cali, sorted-avg.
  *(A web/AI overview claimed GPIO7 / ADC_CHANNEL_6 — WRONG for this board; our CH2 is empirically
  validated: tracks charge, cross-checked against the USB rail.)*
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

## GPIO / expander map (as used by firmware today)
- **PCA9535 @0x20** (I2C-1, owned by display BSP): pin6 = `BAT_READ_EN` (assert ADC sense divider,
  active-high); pin10 = `CHG_ENBn` (charge enable, active-**low**).
- P4 GPIOs: **GPIO15** = charge `STAT` (active-low), **GPIO4** = `VSYS_PG`, **GPIO3** = back button
  (active-low), **GPIO46** = SD VDD switch (`BSP_SD_PWR_EN`).
- SDMMC: single P4 host — SD on **slot 0** (GPIO39–44), C6 esp-hosted on **slot 1** (GPIO6–11).

## OPEN items (do NOT guess — read the schematic or ask)
1. **`ILIM` / `ICHG` resistor values** → the *designed* input-current-limit and charge-current in
   amps. Grep-tracing the nets was unreliable; read them in KiCad (trace the resistors on U16's ILIM
   and ICHG pins) or from a BOM. This is the number that says whether the design intends meaningful
   charging or a token trickle.
2. **Battery capacity** (label/spec) — "~2500 mAh" is unverified.
3. **Full I2C address map** — reconcile the scan addresses above against `I2CTreeDiagram`.

## Corrections this reference supersedes (guessed-wrong facts, now fixed)
- ❌ "Only the IMU + expander are on I2C" → there are ~8–10 devices.
- ❌ "0x40 = INA219 current monitor" (guessed twice) → **ES7210 audio ADC**.
- ❌ "0x40 = GSL3670 touch" (older note) → touch is a different address; 0x40/0x41 = ES7210.
- ❌ "The charger might be I2C-programmable / we can raise the input current limit" → **no, it is
  standalone; ILIM/ICHG are resistors.**
- ✅ "No I2C fuel gauge" — this old note was CORRECT (no MAX17048); 0x36 is some other device, TBD.
