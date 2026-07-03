# E1001 roadmap — bringing the ePaper panel into the system

**Status:** Planning (2026-07-03). Board is on the bench, not yet powered.
**Goal.** Onboard the Seeed reTerminal **E1001** as ADR-0019's **Phase 4 — the abstraction proof**: show that
one server-authored spec drives *both* a P4/LVGL color-touch panel (D1001) and an S3/ePaper status panel
(E1001). Along the way, cash in the D1001 investment ([d1001-development-map.md](d1001-development-map.md)).

**Grounding.** [ADR-0019](../adr/ADR-0019-screen-interface-architecture.md) is the **origin architecture, not a
fixed plan** — it was written at Phase 0. This roadmap carries what we learned building the D1001 (module-first
shared core / ADR-0020; the battery standard / ADR-0024; docs-first hardware discipline). Where they differ,
this wins; ADR-0019 has been amended to point here.

---

## 1. What the E1001 is — schematic-confirmed (V1.2)

**Confirmed against the schematic** `docs/hardware/reTerminal_E1001_V1_2_SCH_251120.pdf` (Seeed, CC BY-SA, 9
sheets, rev v1.2 2025-11-20). Docs-first — these are read off the drawing, not recall.

| Subsystem | Part / detail | Bus / GPIO | Note |
|---|---|---|---|
| MCU | **ESP32-S3R8**, 8 MB PSRAM, native WiFi4/BLE5 | — | no C6/esp-hosted (unlike D1001) |
| Flash | W25Q256 (32 MB) SPI | SPI0 | ample |
| Console/flash | **CH340C** USB-UART + DTR/RTS **auto-reset** | UART0 → `/dev/ttyUSB0` | **esptool/ESPHome flash normally** |
| Charger | **SY6974B** I²C power-path charger (v1.0 was ETA6003) | **I²C1** SDA IO39/SCL IO40, +STAT/CE/QON/NTC | **on I²C** — unlike D1001's off-I²C BQ; read status/current directly |
| Battery sense | ADC via **÷2** divider (10K/10K), load-switch gated | **IO01/VBAT_ADC**, enable **IO21/VBAT_EN** | **same pattern as `ha_battery`** |
| Sys power | VSYS → **TPS631000** buck-boost → 3V3 (1.5 A) | — | same topology family as D1001 |
| Power latch | **CJ3407** P-FET (Q1) + physical slide switch **SW1** | ESP_RST | power-off pattern reusable |
| Battery | 2000 mAh + NTC (J7), 0–45 °C charge window | BAT_NTC | thermal-gate is in the SY6974B (HW) |
| **T/RH** | **SHT40-AD1B-R2** @ **0x44** | **I²C0** SDA IO19/SCL IO20 | onboard room sensor; ESPHome-native `sht4x` |
| RTC | **PCF85063T** @ 0x51 + CR1220 coin cell | I²C0 | deep-sleep wake timing |
| **e-Paper** | SPI EPD (50P **or** 24P FPC; on-board bias DC-DC) | SPI CS IO10/DC IO11/RST IO12/BUSY IO13/SCK IO7/MOSI IO9 | **controller part = on the panel module, NOT in this SCH → must ID** |
| Touch (optional) | I²C touch-panel FPC connector present | I²C1 + INT IO47/RES IO48 | **HW supports touch** — intake said "no touch"; confirm if *our unit* is populated |
| Buttons | **3 user keys** (KEY0/1/2) + BOOT + RST | IO3/IO4/IO5 | matches ADR [a,b,c] |
| LED | 1 green user LED | IO6 | status/warn indicator |
| Buzzer | passive MLT-8530 | IO45/BUZZER_EN | audible alerts |
| Mic | PDM MSM261 (×2) | IO41/42, PDM_EN IO38 | **leave off** (privacy, like the D1001 camera) |
| microSD | SPI (not SDMMC), card-detect | IO7/8/9, CS IO14, DET IO15, EN IO16 | present; E1001 not a recovery node → likely unused |

**Net:** the E1001 is a *standard* S3 board — everything (WiFi, ADC battery, I²C charger/sensor/RTC, SPI
ePaper, buttons, deep-sleep) is bread-and-butter for both ESPHome and ESP-IDF. **The one genuine unknown is the
ePaper controller** (it lives on the panel module, off this schematic) — that gates the display path (§2).

**Still to confirm (small):** the exact EPD controller (power-on log / the panel module p/n / an `esptool`
identify); whether *our unit's* touch layer is populated; and the S3 flash size in practice.

---

## 2. Decision 0 — the platform fork (ESPHome vs ESP-IDF)

**This is the gating decision; everything downstream depends on it.** ADR-0019 §5 left it open; with a shared
C catalog and a battery standard now in hand, it carries real weight.

| | **ESPHome** (Seeed-supported) | **ESP-IDF** (our stack) |
|---|---|---|
| Bring-up speed | **Fast** — Seeed's ePaper + T/H + deep-sleep components exist | Slower — we write the EPD driver + deep-sleep + sensor |
| Abstraction-proof value | **High** — a genuinely second stack rendering the same spec | Lower — same stack as D1001 |
| Firmware C-module reuse | **None** — ESPHome has its own battery/sensor/OTA; reuse is semantic-layer only | **High** — inherits `ha_power_policy`, `ha_battery`, `ha_battery_profile`, the tile-render pattern |
| Battery-standard (ADR-0024) consistency | ESPHome's native deep-sleep battery handling (different model) | our gauge + safety policy, characterized per ADR-0024 |
| Maintenance | a second toolchain (YAML/lambda) | one toolchain; a new MATRIX column |

**Framing / lean — the schematic strengthens the ESPHome case.** Every E1001 peripheral is bog-standard for
ESPHome: native S3 WiFi + MQTT, `adc` battery sense (IO01, ÷2), `i2c` for the SY6974B charger / SHT40 / RTC,
the **ESPHome-native `sht4x`** sensor, `deep_sleep`, `binary_sensor` buttons, `output` for the LED/buzzer.
There's no exotic bring-up (unlike the D1001's P4+C6/esp-hosted, which is exactly why *that* one was forced to
custom ESP-IDF). ADR-0019 also intended the E1001 as *"the deliberate second implementation that validates the
abstraction"* — pointing the same way. The only counter-pull (firmware-module reuse of the battery trio) is
smaller than it looks, because ESPHome covers the battery natively and the ADR-0024 *method* (state-normalized
curve, charge-terminated anchoring) still applies as **calibration data in the ESPHome config**, not lost.

**Recommendation: ESPHome.** The one thing to **de-risk first (the E0 spike):** confirm an ESPHome display
platform supports this ePaper's **controller** (its part is on the panel module, off the schematic — ID it from
the module p/n or a boot log). If a matching ESPHome EPD driver exists (Waveshare/GDEW-class are widely
supported), ESPHome is a clear win; if the controller is unsupported, that single fact is the strongest reason
to reconsider ESP-IDF. **This is Hugh's call**, made in E0 after the EPD-controller check. (Chart still renders
the same BFF spec either way — §3a — so the abstraction proof holds regardless of platform.)

---

## 3. Reuse map — what carries over from the D1001 work

### 3a. Semantic / backend layer — reuses on EITHER platform (the bulk of the win)
This is where "the abstraction" lives, and it is platform-independent:
- **BFF view-models + [shared-ui-spec](shared-ui-spec.md)** — the manifest/tile model (`sensor`/`scene`/
  `alert_banner`/`chart`/status), `vm.traits`/`vm.controls`, `METRIC_CATALOG`. The E1001 renders the same spec.
- **MQTT contracts** — `home/<area>/<id>/state` (both to render *and* to publish its own T/H), `cmd`/`ack`
  (ADR-0010/0014), retained alerts, `GET/POST /control/house/scene`.
- **Capability-descriptor model + per-device key/enrollment + the alert pipeline** (ADR-0019 §3, ADR-0018).

→ Practically all backend work is **reuse**; net-new is a capability-descriptor row + enrollment for the E1001,
and possibly a status-first manifest variant.

### 3b. Firmware C components — reuse ONLY if E1001 = ESP-IDF, and gated by role
| Component | Reuse on ESP-IDF? | Notes |
|---|---|---|
| `ha_power_policy` | **Yes, whole** | board-agnostic — supply a cfg + 4 callbacks (`read_mv`/`power_off`/`warn`/`led`). The module-first payoff. |
| `ha_battery` | **Yes — confirmed ADC sense** | it's ADC (IO01, ÷2), not a fuel gauge → new `_e1001_cfg()`; one small addition — the read-enable is a **direct GPIO (IO21)** vs the D1001's expander pin. **Bonus:** the SY6974B charger is on **I²C**, so a tiny I²C driver gives a cleaner charge-terminated/STAT signal for the 100% anchor than the D1001 had. |
| `ha_battery_profile` | **Yes** | new `_e1001_default()` from an ADR-0024 **characterization run** (deep-sleep changes the load model — offsets differ). |
| `ha_sdcard`, `fs_ops` | **Skip (by role)** | E1001 is deep-sleep → **not** a gapless-recovery node (ADR-0019 §4). No rolling archive. |
| `ha_ble_scan`, `switchbot_decode` | **Skip (by role)** | deep-sleep can't scan continuously → **not** a BLE gateway (ADR-0019 §6). |
| `ha_reach` | **Skip** | edge-mesh census; not a display role. |

→ On ESP-IDF, the **battery trio + the render pattern** reuse; the BLE/SD/recovery stack does **not**, by role,
not by limitation. On ESPHome, none of these apply and battery is ESPHome-native.

---

## 4. New development — E1001-specific (either platform)

1. **ePaper renderer** — the big new piece (nothing like LVGL/MIPI): mono, **partial-refresh**, status-first
   layout of the shared spec. ESPHome: Seeed's display component + lambdas. ESP-IDF: an EPD driver + a mono
   tile renderer.
2. **Deep-sleep power management** — wake schedule (periodic refresh + **button-wake**), snapshot-on-wake,
   battery-frugal refresh cadence. A new paradigm vs the always-on D1001; ePaper *holds its image* while asleep.
3. **Onboard T/H → the bus** — read the sensor and publish `home/<area>/e1001/state`, making the panel *also a
   room sensor* (ADR-0019 §3). This is a real system contribution on day one, independent of the display.
4. **Physical buttons [a,b,c] → actions** — mapped to a few tile actions (scene, ack/dismiss) per the manifest.
5. **Battery** — ESP-IDF: characterize + wire `ha_power_policy`/`ha_battery` (ADR-0024 procedure). ESPHome: use
   its deep-sleep/battery handling; optionally publish SoC to the bus.

---

## 5. Device roles & capabilities — to confirm

Proposed capability descriptor (from ADR-0019 §3; **confirm with Hugh + hardware**):
```yaml
<area>-e1001:
  display: {tech: epaper, color: false, touch: false, refresh: slow, w: 800, h: 480}   # confirm w/h
  power:   deep_sleep            # ~3-month battery; wakes periodically + on button
  roles:   [status, sensor]      # renders status tiles; publishes onboard T/H
  buttons: [a, b, c]             # -> mapped tile actions (scene, ack)
```
- **Is:** a status display + a room T/H sensor; a few button actions; deep-sleep battery.
- **Is NOT** (by role, deliberate): a recovery node, a BLE gateway, or a full touch-control surface.
- **Confirm:** the room/area it lives in, exactly what it should display (its manifest), and whether any
  control (beyond scene/ack) is wanted on the buttons.

---

## 6. Phased plan (mirrors the D1001 arc, adapted)

- **Phase E0 — Scoping + platform decision.** §1 is now schematic-confirmed. Remaining E0 tasks: **(1) back up
  the SenseCraft factory firmware** (image the flash off-git first — the D1001 discipline); **(2) ID the ePaper
  controller** (panel module p/n / boot log) and confirm an ESPHome display platform supports it — *the gating
  spike*; **(3)** check whether our unit's touch layer is populated; **(4) decide ESPHome vs ESP-IDF** (§2).
  *Docs-first; no code yet.* ← **start here**
- **Phase E1 — Beachhead.** WiFi → MQTT (→ OTA if ESP-IDF); ePaper "hello"; **publish onboard T/H** as the
  first real value (it's a sensor immediately). Beachhead-first — prove connectivity before UI (the D1001 rule).
- **Phase E2 — Renderer (the abstraction proof).** Render the BFF spec as **status tiles** on ePaper (partial
  refresh); wire the buttons to manifest actions. *This is the Phase-4 payoff: one spec, two very different
  renderers.*
- **Phase E3 — Deep-sleep + battery.** Wake schedule + power-frugal refresh; battery management (ADR-0024 via
  `ha_power_policy`/`ha_battery` on ESP-IDF, or ESPHome-native). Snapshot-on-wake behavior.
- **Phase E4 — Enrollment + fleet.** Per-device key + capability descriptor + (ESP-IDF) a MATRIX column;
  fold into `provisioning/reterminal/`. Confirm all roles/capabilities live.

---

## 7. Open decisions for Hugh
1. **Platform: ESPHome or ESP-IDF?** (§2 — the gating call; make it in E0 after confirming Seeed support.)
2. **Hardware docs:** where is the E1001 schematic / Seeed BSP / ESPHome config? (docs-first prerequisite.)
3. **Role scope:** status + sensor only, or any button-driven control beyond scene/ack?
4. **Placement:** which room/area, and what should its manifest show?

**First action:** power the E1001 on and identify the board + its vendor support, so E0's platform decision is
made on confirmed facts rather than the intake sketch.
