# ADR-0019 — Screen interface architecture (MCU display panels)

**Date:** 2026-07-01
**Status:** Proposed
**Extends:** ADR-0013 (presentation, API-first) · ADR-0003 (WASM firmware split) · ADR-0014 (device-control
conventions) · ADR-0016/0018 (failover history / record-keeping nodes)

## Context

We are bringing a fleet of Seeed reTerminal display panels onto the HA system as physical, per-room
control/status surfaces:
- **reTerminal D1001** — ESP32-**P4** (400 MHz dual RISC-V + LP core, 32 MB PSRAM, 32 MB flash) + ESP32-**C6**
  WiFi-6/BLE coprocessor via `esp-hosted`; 8" 1280×800 **color touch** LCD; microSD; battery; camera
  (disabled). Always-on / mains-capable.
- **reTerminal E1001** — ESP32-**S3** (native WiFi); 7.5" **mono ePaper** (seconds-slow refresh); onboard
  **T/H sensor** + buzzer; microSD; **deep-sleeps** for ~3-month battery.

The goal (Hugh): *"design the architecture of a screen interface for this HA system in general, then allow
implementation at a per-device level,"* with the app **updatable without reflashing**, network-lean, and the
panels doubling as **local data-recovery nodes**. These two devices are deliberately very different (fast
color touch vs. slow mono ePaper; P4+C6 vs. S3), which is exactly why one shared contract + per-device
renderers is the right shape.

ADR-0013 already anticipated this device class ("MCU-driven displays … that cannot run a browser, e.g. Seeed
D1001/E1001") and laid the foundation: an API-first backend, per-client BFF view-models, and panels as MQTT-
native clients. This ADR is the panel-specific layer on top of that accepted foundation.

## Decision

### 1. A panel is "the PWA, in firmware." Reuse the live contract; do not reinvent it.

The PWA is already **declarative-by-trait**: it fetches BFF view-models and renders cards purely from
`vm.traits` + `vm.control.strategy`, with no per-device hardcoding. A panel does the same. **The BFF
view-model IS the device-agnostic screen descriptor** — panels consume the existing, LIVE surfaces:

| Need | Existing surface (LIVE) | Notes |
|---|---|---|
| Full/initial state | `GET /api/v1/displays`, `/display/{id}`, `/sensors`, `/alerts`, `/house` | same as the PWA |
| Realtime telemetry | MQTT `home/<area>/<device_id>/state` | panels subscribe natively — no SSE bridge needed |
| Alerts | MQTT `home/_alerts` (retained) + `home/_alert/new` (edge) | banner |
| Commands | signed HMAC → `home/<area>/<device_id>/cmd`, await `…/cmd/ack` | ADR-0010/0014 protocol; per-device secret |
| Scenes | `GET/POST /control/house/scene` | Home/Away/Sleep |
| Room scoping | registry `area` + `device_meta.room` overlay | per-panel filter |

New backend work is therefore **minimal** — mostly finishing the half-built **per-device key** for panel-side
auth (bearer on the BFF GETs; the command HMAC path already exists server-side).

### 2. Layered device model — a stable host + a swappable app (panel-derived, manifest-driven).

Per Hugh's steer (update the app without reflashing; keep it network-lean; render locally), the panel splits
into layers with very different change cadences:

| Layer | Changes | Delivery | Contents |
|---|---|---|---|
| **Firmware host** | rarely | cable-flash once → OTA for new primitives | `esp-hosted`/MQTT client, HTTP client, LVGL **renderer + fixed tile primitives**, OTA, the data agent (§4) |
| **Panel app** | often | **fetched manifest — NO reflash** | a declarative UI manifest: which rooms, ordered tiles, device/metric bindings, brightness/night |
| **Live data** | constantly | MQTT deltas + BFF for full state | rendered into the tiles |
| **Commands** | on tap | signed HMAC → `…/cmd` | trait/action from the tile |

The firmware ships a **fixed library of tile primitives** — `sensor`, `actuator` (renders from `vm.traits`),
`scene`, `alert_banner`, `chart`. What each panel *shows* is a small **declarative manifest it fetches from
the server and renders locally** (panel-derived layout). Changing a panel's rooms/tiles/layout = edit server
config → panel re-fetches → re-renders. **Reflash is needed only to add a brand-new tile *type*.**

Illustrative manifest (fetched once + on change; live values ride MQTT, so this is network-negligible):

```json
{
  "manifest_v": 1,
  "panel": "office-d1001",
  "rooms": ["office"],
  "tiles": [
    {"type": "sensor",   "device": "aranet_office", "metrics": ["co2_ppm", "temperature_c", "humidity_pct"]},
    {"type": "actuator", "device": "levoit_office"},
    {"type": "chart",    "device": "aranet_office", "metric": "co2_ppm", "window": "24h", "source": "local"},
    {"type": "scene"},
    {"type": "alert_banner"}
  ]
}
```

This is **tier (b)** of a deliberate spectrum. **Tier (c)** — the whole app (layout *and behavior*) as an
OTA-loadable sandboxed **WASM module** — is the [ADR-0003](ADR-0003-wasm-firmware-split.md) endpoint. The P4
(400 MHz, 32 MB PSRAM) removes ADR-0003's bare-C6 RAM worry, so (c) is realistic *later*; we architect the
host so a WASM app module is a clean bolt-on, but v1 targets (b) at a fraction of the effort.

### 3. Capability profiles — one contract, per-device rendering.

Each panel carries a capability descriptor that decides *how* the shared contract renders and what roles it
can hold:

```yaml
office-d1001:
  display: {tech: lcd, color: true, touch: true, refresh: fast, w: 1280, h: 800}
  power:   always_on           # mains-capable
  roles:   [control, recovery] # recovery-eligible (§4)
hallway-e1001:
  display: {tech: epaper, color: false, touch: false, refresh: slow, w: 800, h: 480}
  power:   deep_sleep          # ~3-month battery; wakes periodically
  roles:   [status, sensor]    # publishes onboard T/H; NOT gapless-recovery-eligible
  buttons: [a, b, c]           # physical buttons → mapped tile actions
```

- **D1001** (fast color touch): full interactive tiles, live control, charts, recovery node.
- **E1001** (slow mono ePaper, deep-sleep): status-first read-mostly rendering; physical buttons mapped to a
  few actions (scene, ack); **publishes its onboard T/H back onto the bus** as a room sensor
  (`home/<area>/<id>/state`) — a panel that is also a sensor.

### 4. Panels as local data-recovery nodes (D1001) + async local cache.

A background **data-agent task** on the P4's second core (fully decoupled from the UI thread) subscribes to
the **full `home/+/+/state` stream** and persists it to **microSD** as a rolling archive, batched (buffer in
PSRAM, flush every few seconds → large sequential writes, minimal wear). This gives two properties:

- **Async display from local cache:** tapping a `chart` tile reads history from **SD locally** — instant,
  and it works **offline**. The UI never blocks on network or I/O.
- **Distributed recovery:** each always-on panel is a rolling redundant copy of system data — a tier *below*
  the warm standby, but far better than today's "GATT device-pull, slow/partial last resort." The
  reconcile tooling (ADR-0016 `reconcile-history.sh`, ADR-0018 record-keeping) may pull a panel's SD archive
  as a recovery source of last resort.

**microSD is required** for this role. Onboard 32 MB flash is firmware/app only; PSRAM is volatile. Spec: a
**high-endurance (dashcam-rated) microSD**, ~32 GB (years of retention + wear headroom for 24/7 batched
writes); the card being **physically removable** is itself a recovery win (pull it, read it in a card reader
even from a dead panel).

**Honest constraint:** continuous-capture recovery belongs to **always-on panels (D1001)**. The **E1001
deep-sleeps** for battery life and *cannot* capture the stream gaplessly — it does periodic snapshots at
best. The capability profile marks recovery eligibility (`roles: [recovery]`) accordingly.

### 5. Firmware stacks.

- **D1001:** ESP-IDF + **LVGL** + `esp-hosted` (C6 radio) + MQTT + HTTP + OTA + SD data agent. Camera driver
  never initialized (privacy + power).
- **E1001:** **ESPHome** (Seeed-supported on the E-series) or ESP-IDF, rendering the *same* manifest/tile
  model to ePaper. It is the deliberate second implementation that **validates the abstraction**: if one
  manifest drives both a P4/LVGL touch panel and an S3/ePaper display, the design holds.

## Consequences

- The backend is **mostly reuse** — the big win. New server work: finish the per-device panel key; optionally
  a small per-panel manifest store/endpoint (`GET /api/v1/panel/<id>/manifest`) and panel registry entry.
- Panels are **first-class MQTT clients + record nodes**, deepening system resilience (more distributed data
  copies) at near-zero backend cost.
- The stable-host / swappable-app split bounds reflash risk and makes routine UI changes a server-side edit.
- A new firmware platform to build/maintain (ESP-IDF/LVGL on P4 + esp-hosted); real but one-time.

## Tradeoffs accepted

- **Panel-derived over server-scoped layout:** a server-scoped view would be marginally leaner per-refresh,
  but live data rides MQTT deltas (the real traffic), so the difference is moot; panel-derived keeps layout
  flexible via OTA/manifest without central-render coupling.
- **Tier (b) now, not (c):** a declarative-JSON renderer gets ~90% of the "no-reflash" flexibility without
  building a WASM app runtime yet. We pay a small re-architecture cost if/when we adopt (c).
- **Recovery asymmetry:** only always-on panels are gapless recovery nodes; deep-sleep ePaper cannot be.

## Phased plan

- **Phase 0 — Design (this ADR).** + manifest schema draft + capability-profile model + the reuse contract
  above. *No hardware risk.*  ← **we are here**
- **Phase 1 — D1001 host beachhead.** ESP-IDF image: `esp-hosted` C6 → WiFi → MQTT `.210` → **OTA proven**,
  camera disabled, display "hello". Prove connectivity + OTA *before* UI (beachhead-first, per the Levoit).
- **Phase 2 — Renderer + manifest.** LVGL tile primitives (`sensor`/`actuator`/`scene`/`alert_banner`/
  `chart`) driven by a fetched manifest; consume BFF view-models + MQTT deltas; signed command publish;
  area-scoped. First real control panel.
- **Phase 3 — Data agent + recovery node.** Background SD data agent (full-stream subscribe, batched rolling
  archive, on-demand local history for charts); hook into reconcile tooling as a last-resort source.
- **Phase 4 — E1001 as the abstraction proof.** Same manifest/tile model on ESPHome/ePaper (S3); onboard T/H
  published as a sensor; deep-sleep snapshot behavior.
- **Phase 5 — Fleet rollout.** Per-room panels; `provisioning/reterminal/` runbook; panel enrollment
  (per-device key); central manifest management.
- **Future — tier (c).** ADR-0003 WASM app module for downloadable *behavior*, not just layout.

**Labor:** dev is heads-down on the OpenWRT router cutover; ops + Hugh drive Phase 0–1; pull dev in for the
firmware phases once the network is settled.

## Open questions

- Manifest delivery: static per-panel file vs. a small `GET /api/v1/panel/<id>/manifest` endpoint + registry.
- On-SD archive format: append-only day logs vs. a mini two-tier (sqlite hot + parquet) mirroring ADR-0006.
- Exactly how the reconcile tooling treats a panel archive (partial/rolling window) as a recovery source.
