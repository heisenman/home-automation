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

## 1. What the E1001 is — documented vs must-confirm

**Docs-first discipline (a D1001 lesson): assert no hardware fact from recall.** The rows below marked *intake*
come from the device-intake (`e9ee4f6`) and ADR-0019; everything must be **confirmed against the E1001
schematic / on power-on** before any code depends on it.

| Aspect | From intake (ADR-0019) | Must confirm (schematic / power-on) |
|---|---|---|
| MCU | ESP32-**S3**, native WiFi (no C6/esp-hosted) | exact variant, flash + PSRAM size |
| Display | 7.5" **mono ePaper**, ~800×480, seconds-slow refresh, no touch/color | EPD controller part; **partial-refresh** support; exact resolution |
| Sensors | onboard **T/H** + buzzer | sensor part(s) + I²C address/bus; buzzer control |
| Power | **deep-sleep**, ~3-month battery; microSD | battery **gauge/charger topology** — is there an ADC sense? a fuel-gauge IC? charger part? power-latch? |
| Input | 3 physical **buttons** [a,b,c] | button GPIOs + wake-capability |
| Vendor support | **ESPHome** (Seeed-supported on E-series) *or* ESP-IDF | does Seeed ship an ESPHome config **and/or** an ESP-IDF BSP? |

**First action (docs-first):** power it on, find/point to the E1001 **schematic + Seeed BSP/ESPHome config**
(as we had `D1001_Docs/` + `reTerminal-D1001` BSP), and confirm the table. Missing a doc → ask, don't guess.

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

**Framing / lean.** ADR-0019 intended the E1001 as *"the deliberate second implementation that validates the
abstraction"* — which points at ESPHome, and E1001's role (status-first, read-mostly, deep-sleep, onboard T/H)
is squarely what ESPHome + Seeed's components do well and fast. The counter-pull is firmware-reuse + one battery
standard, which favor ESP-IDF. **Recommendation: default to ESPHome for a fast abstraction proof** (semantic
reuse is the big reuse anyway — see §3), and reserve ESP-IDF for if/when we need the C safety modules on this
board. **This is Hugh's call** and should be made in Phase E0 once the hardware + Seeed support are confirmed.

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
| `ha_battery` | **Mechanism yes** | new `_e1001_cfg()` for its ADC/charger wiring **iff** it has an ADC sense; if it has a fuel-gauge IC instead, that's a *new* driver behind the same interface. |
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

- **Phase E0 — Scoping + platform decision.** Power on; confirm §1 against the schematic/Seeed support;
  **decide ESPHome vs ESP-IDF** (§2). *Docs-first; no code.* ← **start here**
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
