# firmware/ — shared edge/panel firmware core (ADR-0020)

**Target tree** for the module merge that retires the `edge/*/main/` `cp -r` fork tax. Populated
**incrementally**, panel-first, lowest-risk module first. Until a module lives here, its canonical copy is
still the fork under [edge/](../edge/AGENTS.md); [MATRIX.md](../edge/MATRIX.md) is the source of truth for
which build links which.

```
firmware/
  components/<module>/     real shared IDF components (header states contract + platform support)
    <module>.c
    include/<module>.h      public header
    CMakeLists.txt          idf_component_register(...)
    README.md               contract, platform support, ADR ref
    test/                   host unit test + run.sh (no IDF needed) where the module is pure
  devices/<device>/        (future) thin per-device builds: platform shim + REQUIRES-picked modules
```

## ⛔ MODULE-FIRST — the mandate this tree exists to serve (ADR-0020)

*This is Principle 3 (decompose / module-first) from the [root AGENTS.md](../AGENTS.md) — stated once there,
repo-wide; its firmware detail lives here. Link up, don't duplicate.*

**New capability = a new module. Never new lines in a god-file, never a fourth `cp -r` copy.**

This is not a style preference; it is the standing rule for all firmware work.

**⛔ HARD LINE (Hugh, 2026-07-03): every new feature goes through a modularity-decomposition step
BEFORE any code is written.** Answer "how much of this becomes modules?" up front — name the shared
reusable modules vs. the thin device-specific glue, and the boundaries/interfaces (injected
actuators, config) — as part of the design/ADR/plan. No decomposition, no dev. (Proven on the
battery-power-policy work: [ADR-0024](../docs/adr/ADR-0024-battery-monitoring-standard.md) split it
into `ha_battery` / `ha_battery_profile` / `ha_power_policy` + board glue before a line was written.)

Before adding a feature, extract the seam **first**:

- **Shared across devices** → a real IDF component under `firmware/components/<module>/` with a
  header contract + platform-support note (this tree). Platform differences become caller hooks/cfg
  structs, not forks. A new device is a **column in [MATRIX.md](../edge/MATRIX.md), not a fork.**
- **App-local** (one build's internals) → an in-app unit: its own `.c/.h`, private statics, a header
  that states the contract. Precedent: the D1001 panel's `ui_tiles.c` went **1393→206L** behind 8
  `ui/` modules ([panel-ui-modularization](../docs/design/panel-ui-modularization.md)) with zero
  behavior change — and only *then* was memory-refactored cleanly.

A file that has grown a second responsibility, or that a feature would bloat further, is a
**review-blocking defect** — fix the structure before adding the behavior. When unsure where a seam
belongs, extract app-local first; promote to a shared component when a second device needs it.

## Migration status (ADR-0020 Stage 1)

| Module | Home | Consumed by | Notes |
|--------|------|-------------|-------|
| `switchbot_decode` | **`firmware/components/`** ✓ | **all builds** (panel + c3/c6/s3) | pure, host-tested; verbatim lift. Fork copies retired fleet-wide. |
| `ha_ble_scan` | **`firmware/components/`** ✓ | **all builds** (panel + c3/c6/s3) | shared NimBLE observer; controller-init + publish sink + `shared_radio` duty-cycle are caller hooks/flags (native vs VHCI; WiFi coexistence). The s3 duty-cycle drift is reconciled here. Fork copies retired. |
| `ha_sdcard` | **`firmware/components/`** ✓ | d1001-panel | microSD mount (SDMMC+FAT); board pins/power/LDO are `ha_sdcard_cfg_t`. Coexists with the C6 SDIO (shared P4 SDMMC host). |
| `fs_ops` | **`firmware/components/`** ✓ | d1001-panel | generic SD file-ops over MQTT (ls/stat/read/write/rm/mkdir/df); device-agnostic. |
| `ha_battery` | **`firmware/components/`** ✓ | d1001-panel | gauge (ADC→SoC) + IMU temp + thermal-gated charge + restart watchdog; board wiring is `ha_battery_cfg_t` (`_d1001_cfg()` preset), expander/I2C handles injected by the display BSP. |

**Fully migrated (ADR-0020 Stage 2 complete for the BLE core).** Consumers link the shared components via
`REQUIRES` + `EXTRA_COMPONENT_DIRS ../../firmware/components` (edge nodes) or `components/<name>` **symlinks**
(the panel, off-repo dev tree at `~/reterminal-dev/d1001-beachhead`). Each node keeps a 2-line `ble_scan.h`
shim so `gatt_*`/`ha_ota` includes are untouched. Still forked (future stages): `ha_mqtt` (3×-drifted),
`app_main`, `gatt_*` (Stage 2 → shared `ha_gatt`), and the platform modules. Extraction order + rationale: [edge/MODULES.md](../edge/MODULES.md).

## Rules

- **Additive first.** Create the shared component and prove it in isolation *before* pointing any build at
  it. Live edge nodes migrate **gated, one at a time, re-validated** (Stage 2) — never big-bang.
- **Pure modules ship a host test** (`test/run.sh`, plain `cc`, no IDF) so correctness is provable off-target.
- A device is a **column in [MATRIX.md](../edge/MATRIX.md), not a fork.** When the generator lands, that
  table is produced from each build's `CMakeLists REQUIRES` and CI-checked against reality.
