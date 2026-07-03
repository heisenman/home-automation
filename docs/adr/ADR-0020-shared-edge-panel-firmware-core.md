# ADR-0020 — Shared Edge/Panel Firmware Core (Module Catalog + Device Matrix)

**Date:** 2026-07-01
**Status:** Accepted (2026-07-02)

## Decision

Promote the [FIRMWARE-GUIDE](../../edge/FIRMWARE-GUIDE.md) module map from a copy-paste (`cp -r`) fork
convention into **real shared ESP-IDF components**, consumed by every ESP device — the edge nodes
(`esp32c3`/`esp32c6`/`esp32s3-eth`) **and** the reTerminal panels (D1001/E1001). Each device build becomes a
**thin platform shim** (radio-init + transport) that selects modules from a shared catalog. The composition is
recorded in a **module catalog** (`edge/MODULES.md`) and a **device × module matrix** (`edge/MATRIX.md`) that is
**generated from the builds' `CMakeLists` `REQUIRES`** so it cannot silently drift.

## Context

The edge firmware is *already* modular by design (`ha_config`, `ha_wifi`/`ha_eth`, `ha_sntp`, `ha_mqtt`,
`ble_scan`, `ha_relay`, `switchbot_decode`, `gatt_exec`/`gatt_history`, `ha_ota`, `app_main`) — but distributed
as **duplicated copies**: `switchbot_decode`/`gatt_*`/`ha_relay` are byte-identical across c3/c6/s3, while
`ha_mqtt`/`app_main`/`ble_scan` have drifted 2–3 ways. The git log records the tax directly ("esp32c3 node —
fork of esp32c6", "port ha_relay to the C6 + C3 **forks**"). The D1001 panel (ADR-0019) is now BLE-capable
(NimBLE-over-esp-hosted-VHCI, [C6-SLAVE-FLASH-PROCEDURE](../../provisioning/reterminal/C6-SLAVE-FLASH-PROCEDURE.md))
and Stage 1 would otherwise add a **4th copy** of the decode+observer logic.

The key enabler: edge nodes and the panel run the **same NimBLE API on the same IDF v5.4**. The only genuine
difference is BLE controller init — native controller (edge) vs `esp_hosted_bt_controller_init` + VHCI (panel) —
and the transport (native WiFi / W5500-eth vs esp-hosted-WiFi). Everything above that line is identical logic.

This is the firmware analog of the presentation merge (ADR-0013): merge at the **logic layer**, keep thin
platform adapters. Unlike DOM-vs-LVGL there is no renderer wall here, so the merge is cleaner.

## Design

- **`firmware/components/<module>/`** (target layout; migrate out of `edge/*/main/` incrementally): each module
  is a real IDF component with a header that states its contract + platform support.
- **`firmware/devices/<device>/`**: thin builds — pick modules via `CMakeLists REQUIRES` + one platform shim
  (radio-init, transport). A new device is a **column, not a fork**.
- **`edge/MODULES.md`** — the catalog: module → role, contract/ADR ref, platform support
  (native-radio / esp-hosted-VHCI / host-only), deps, WASM-candidacy (→ ADR-0003).
- **`edge/MATRIX.md`** — device × module matrix, **generated from `CMakeLists REQUIRES`** (a checker fails CI if
  the doc and the builds disagree — same drift-guard philosophy as `test_viewmodel` pinning the UI catalog).

The panel, linking the shared core, becomes a **full peer edge node**: advert relay to
`home/edge/<node>/<mac>/adv`, GATT history/exec, signed commands (ADR-0010), ADR-0015 relay coverage — all for
free, and the dictator's `edge-mapper` ingests it as just another `<node>` with **zero new server work**.

## Phasing (blast-radius-aware — edge nodes are LIVE)

1. **Stage 1 (panel-first, low risk):** extract `switchbot_decode` + `ha_ble_scan` (observer) into shared
   components; the **panel adopts them first** (bench/dev), publishing `home/<area>/<id>/state`.
2. **Migrate live edge nodes** (c3/c6/s3) onto the shared components — **gated**, re-validated per node,
   retiring the copies. Reconcile the drifted `ha_mqtt`/`app_main`/`ble_scan` into one parameterized module.
3. **GATT (Stage 2):** shared `ha_gatt` (history + exec) for the panel.

## Consequences

- One canonical BLE/relay implementation: fix a decoder once, every node + panel gets it.
- Retires the `cp -r` fork tax; new devices (E1001, non-Seeed) compose from the catalog + a platform shim.
- Panel is a **constrained** edge node: BLE rides esp-hosted VHCI over SDIO shared with WiFi → the
  FIRMWARE-GUIDE §3 coexistence duty-cycle applies harder; weaker internal antenna (SMA pending).
- Migrating live nodes carries re-validation cost → strictly gated, panel proven first.
- **Bridges ADR-0002 ↔ ADR-0003:** build-time modules *produce* the ADR-0002 runtime traits; a clean component
  today is the exact unit ADR-0003 would later repackage as an OTA WASM module (catalog entry unchanged).

## Acceptance (2026-07-02)

Accepted — Stage 1 and the BLE core of Stage 2 are **live**. `firmware/components/` now holds the
real shared components — `switchbot_decode`, `ha_ble_scan`, `ha_sdcard`, `fs_ops`, `ha_battery`,
`ha_reach` (+ the `sgp30`/`sgp40`/`sensirion_gas_index` gas stack) — linked by both the edge nodes
(c3/c6/s3) and the D1001 panel via `REQUIRES`; the `switchbot_decode`/`ha_ble_scan` fork copies are
retired fleet-wide. The catalog ([edge/MODULES.md](../../edge/MODULES.md)) and device×module matrix
([edge/MATRIX.md](../../edge/MATRIX.md)) exist. The panel links the shared core as a **peer edge
node** (advert relay + reach), as designed — zero new server work.

Remaining stages proceed under this accepted direction (not blockers to acceptance): reconciling the
3×-drifted `ha_mqtt`/`app_main`, the shared `ha_gatt` (Stage 2 GATT), the `MATRIX.md` generator +
CI drift-check, and the `firmware/devices/<device>/` thin builds. The **module-first** principle this
ADR institutionalizes — new capability = new module, never a fourth copy or a fatter god-file — is
now a standing rule in [firmware/AGENTS.md](../../firmware/AGENTS.md); the D1001 panel's in-app UI
split (`ui_tiles.c` 1393→206L behind 8 `ui/` modules,
[panel-ui-modularization](../design/panel-ui-modularization.md)) is the app-local face of the same
principle.

## Rejected alternatives

- **Keep copy-paste forking:** O(n) maintenance, guaranteed drift (already happening).
- **Port-by-copy into the panel (4th copy):** entrenches the tax the day we have a clean chance to remove it.
- **Full WASM split now (ADR-0003):** the ambitious end-state; the shared native-component catalog is the
  prerequisite substrate and delivers value immediately without the WASM RAM/energy cost.
