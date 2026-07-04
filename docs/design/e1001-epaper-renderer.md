# E1001 ePaper renderer — decomposition (implements ADR-0019 Phase 4, the abstraction proof)

**Date:** 2026-07-03  **Status:** Design (decompose-before-dev; no render code until this is blessed).  **Owner:** ops.
Platform is **locked to ESPHome** (E0 resolved: panel `HT075A04` = 7.5" mono 800×480, controller **UC8179**,
native `display: waveshare_epaper / 7.50inv2`). This doc designs the *renderer* that sits on top of the
[beachhead](../../provisioning/reterminal/e1001/e1001.yaml). Grounded in the real BFF contract in
`server/api/viewmodel.py`, not the ADR's illustrative snippet. See also [e1001-roadmap.md](e1001-roadmap.md),
[shared-ui-spec.md](shared-ui-spec.md), [[e1001-onboarding]].

## The decision this implements (ADR-0019, Hugh: "0019 takes precedence")
The E1001 **renders locally from the shared server-authored spec** (ADR-0019 **tier (b)**), it does NOT display
a server-rasterized frame. The rejected "server paints a 1-bit BMP, device blits it" model is exactly the
**central-render coupling** ADR-0019 §Consequences avoids. One spec → two renderers (D1001/LVGL, E1001/ePaper)
is the Phase-4 "abstraction proof." The reused thing is the **contract**, not code (LVGL primitives can't paint
ePaper; the ePaper tile library is new — mirrors the D1001's tile *semantics*).

## The contract we consume (real fields, `server/api/viewmodel.py`)
No new server endpoints (ADR-0019: "no panel-specific server endpoints"). The renderer reads what the PWA/D1001
already read:
- **`GET /api/v1/sensors`** → `{ sensors: [{device_id, device_type, area, ts, metrics:{<key>:value}, offsets}],
  metrics: [<metric_spec>] }` (`main.py:881`). `metric_spec = {key, label, unit, color, precision, graph}`
  (`METRIC_CATALOG`). Presentation (label/unit/precision/**order**) is server-authored — the mono panel ignores
  `color` (1-bit) but honors the rest, so the spec still drives it.
- **`GET /api/v1/alerts`** → `{ alerts: [{severity: critical|warning|info, kind, device_id, name, detail}] }`
  (`main.py:929`, `build_alerts`, already severity-sorted). **Separate endpoint — NOT on the sensors payload.**
  Also published **retained** to an MQTT alerts topic (`main.py:124`), so a just-woken device can get the banner
  input from retained MQTT without an HTTP round-trip — the one place retained-MQTT clearly beats the snapshot.
- **`GET /api/v1/displays`** → per-actuator `{device_id, name, vm.controls, override, last_decision}` (status
  view only for E1001 — read-mostly; no touch control widgets in v1).
- **`GET /api/v1/house`** → scenes (active scene for the header; buttons map to scene set/ack).

## Data-source model — the key departure from the D1001
The D1001 is always-on and holds an MQTT subscription for **live deltas**. The E1001 **deep-sleeps** and cannot.
So the E1001's value source is a **snapshot HTTP GET per wake**, not a live subscription:

> **wake → (wifi up) → `GET /api/v1/sensors` + `/api/v1/alerts` (+ `/house` if due) → render once → deep-sleep.**
> (Alerts may instead come from the retained MQTT alerts topic on wake — saves one round-trip; see contract.)

Same view-models, different transport — the abstraction still holds. (Alternative considered: subscribe to
retained MQTT state on wake. HTTP snapshot is simpler, gives one coherent frame, and matches "fetch once per
wake." Retained-MQTT stays open as an optimization if wake-time latency hurts.) In **dev mode** (always awake,
`deep_sleep` disabled) we poll on a timer so the render loop is OTA-iterable without sleep cycles.

## Implementation vehicle — native ESPHome, not an external component (decided 2026-07-03)
Two ways to add custom logic in ESPHome: (A) a formal **external component** (Python codegen + C++ + registration)
or (B) **native YAML** (`http_request` + `json` + a display `lambda`). **Both compile to the same native C++ in
one binary → identical runtime perf and power** (ESPHome codegen isn't interpreted at runtime); the only
difference is code organization. The battery cost lives entirely in the wake cycle (WiFi + fetch + the ~5 s
blocking ePaper refresh), which is identical either way. So we build **native (B)** — fastest to a working
spec-driven panel on the proven OTA loop — and extract to an external component only if a *second* ePaper device
ever needs to share the code. The four seams below still structure the work; they're just realized as
`http_request` (fetch) → `json::parse_json` into globals (parse) → display `lambda` (layout + tiles) rather than
C++ classes. `components/e1001_ui/` keeps the data-model header + fixtures for that potential later extraction.

## Seam decomposition (module-first, realized natively)
ESPHome gave us the platform but **no declarative tile engine** (LVGL was that for the D1001). Four seams, each
independently reasoned:

1. **Spec ingestion** (`spec_client`): an HTTP GET + JSON parse (ArduinoJson, already in ESPHome) producing an
   in-RAM `PanelModel { sensors[], metrics[], alerts[], scene }`. Pure transform, host-testable against a
   captured JSON fixture. Owns nothing about drawing.
2. **Tile-primitive library** (`tiles`): fixed set, each a `draw(display, rect, data)` fn on the 1-bit buffer —
   `sensor_tile`, `alert_banner`, `scene_header`, `status_footer`. Mirrors the D1001 tile *types*; painted for
   mono 800×480. No layout logic inside a tile (it fills its given rect).
3. **Layout** (`layout`): turns the `PanelModel` + panel geometry into a list of `(tile, rect, data)`. v1 =
   fixed status layout (banner ▸ header ▸ 2-col sensor grid ▸ footer); later, driven by a per-panel manifest
   from the capability profile (ADR-0019 §3) so tile *selection* is server-authored too.
4. **Refresh coordinator** (`refresh`): owns the ePaper's slow-refresh reality. `refresh: slow` ⇒ **full refresh
   on wake**; in dev-mode, **partial** refresh for value-only changes with a **full-refresh every N updates** to
   clear UC8179 ghosting. Never redraw-per-delta.

```
components/e1001_ui/
  __init__.py         # ESPHome codegen: config schema (endpoint, wake_interval, layout) → C++
  e1001_ui.h / .cpp   # component: owns spec_client + layout + refresh; hooked from the display lambda
  spec_client.h/.cpp  # HTTP GET + JSON → PanelModel   (host-testable, pure parse)
  tiles.h/.cpp        # sensor_tile / alert_banner / scene_header / status_footer
  layout.h/.cpp       # PanelModel → [(tile, rect, data)]
  test/               # host tests: fixture JSON → PanelModel; layout geometry (no ePaper needed)
```

## Layout v1 (800×480, 1-bit)
- **Alert banner** (top, ~64px, only if alerts): critical = inverted (black fill / white text); warning/info =
  outlined. Text = `name — detail` from `build_alerts`, highest severity first.
- **Scene header** (~40px): active house scene name + clock + wifi/battery glyphs.
- **Sensor grid** (body): 2 cols × rows of `sensor_tile`, each = room/name + up to 3 metrics rendered
  `label  value unit` at `metric_spec.precision`, in catalog order. ~3–4 rows visible.
- **Status footer** (~28px): last-update time + battery %.
- **Fonts:** baked from a TTF at ~3 sizes (value ~44, label ~20, small ~14). **Must include the unit glyphs**
  the catalog uses: `° µ ³ ² ₂ %` (CO₂, µg/m³, °C) — easy to miss; verify the glyphset or units render as tofu.

## Wake, power budget & buttons (deep-sleep sub-design)
**Wake sources.** Two, via ESPHome `deep_sleep`:
- **Timer** (primary): `sleep_duration = wake_interval` (~15 min default; the battery/freshness knob). RTC timer.
- **Buttons** (on-demand): GPIO3/4/5 are RTC-capable (S3 RTC GPIO = 0–21) → EXT1 wake. Buttons are **active-low**
  (to GND), and the **S3 supports `ESP_EXT1_WAKEUP_ANY_LOW`** (classic ESP32 didn't) → wake when any button is
  pressed. **Caveat to verify on HW:** the pins must hold high during sleep — needs **RTC-domain pullups** unless
  the board has external pull-ups (schematic didn't show them on IO3/4/5). If RTC pullups misbehave in sleep,
  ship **timer-wake first**, add button-wake as a follow-on. Do NOT block P4e on it.

**Power budget (model now, numbers from the bench — ADR-0024, do NOT borrow D1001's).** Battery life is
dominated by two unknowns that must be **measured**, not guessed: (1) **board sleep current** (S3 deep-sleep is
~tens of µA, but the TPS631000 buck-boost + SY6974B charger + CH340 quiescent set the real floor — likely the
dominant term); (2) **charge per wake cycle** = wifi-reconnect + snapshot fetch + one UC8179 full refresh, all
active for a few seconds. Life ≈ `capacity_mAh / (sleep_mA + wakes_per_day × charge_per_wake_mAh/24)`. The design
lever is **minimize active time** (fast wifi reconnect — cache BSSID/channel; the ePaper holds its image with the
radio off, so nothing is drawn while connecting) and **lengthen `wake_interval`**. Get `capacity_mAh` from the
cell label; measure sleep_mA + per-wake charge on the bench before quoting a "~3-month" figure.

**Battery gauge.** v1 = the beachhead's ADC voltage (GPIO1 ×2, enable GPIO21) + a simple V→% for the footer.
Reuse of `ha_power_policy`/`ha_battery_profile` is **later** (they're ESP-IDF C; wrappable as an ESPHome external
component under the esp-idf framework, but v1 doesn't need the safety state-machine — deep-sleep device, user
recharges over USB). The E1001's **SY6974B charger is on I2C1** (unlike the D1001's off-I2C BQ) → STAT/current
are *readable*, so a later charge-state tile is cheap. Characterize the E1001 cell on its own (ADR-0024).

**Button map (3 buttons; refine with Hugh).** Green (GPIO3) = **refresh now** (wake→fetch→render) / page-cycle;
Right/Left (GPIO4/5) = page nav / alert-ack / scene-select. **A held combo = "stay awake for OTA"** — mandatory
on a deep-sleep device (otherwise firmware can't be pushed): the device wakes, sees the hold, skips `deep_sleep`,
and stays up so `esphome run` (OTA) can reach it. Mirrors the beachhead's dev-mode-awake idea.

## Phasing (each OTA-iterable after the first serial flash)
- **P4a — beachhead** ✅ drafted: wifi/ota/mqtt/captive_portal + SHT4x + battery + I2C scan. Prove join on `.210`.
- **P4b — renderer skeleton:** `e1001_ui` draws one hard-coded `sensor_tile` from a static `PanelModel`. Proves
  the display path (UC8179 full refresh) + the component hook. No network.
- **P4c — spec ingestion:** `spec_client` GETs `/api/v1/sensors`, `layout` fills the sensor grid from real data.
- **P4d — alerts + scene + footer:** `build_alerts` banner, `/api/v1/house` header, status footer.
- **P4e — deep-sleep + buttons:** wake→fetch→render→sleep loop; buttons → scene/ack + OTA-hold. Battery footer.
- **P4f (optional) — server-authored layout:** per-panel manifest from the capability profile drives tile
  selection (full ADR-0019 §3), not just a fixed status layout.

## Open decisions for Hugh (before P4b code)
1. **ESPHome external component** (`e1001_ui`, above) vs. a big display lambda + globals. Recommend the
   component — it's the only shape that makes the tile library a real, testable module (decompose-before-dev).
2. **Snapshot HTTP-per-wake** (recommended) vs. retained-MQTT-on-wake for values.
3. **v1 fixed status layout** (recommended, ship P4b–P4e) vs. holding out for the server-authored manifest (P4f)
   from the start. Fixed-first is faster and the manifest bolts on without rework.
