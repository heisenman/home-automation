# D1001 development map — how the reTerminal panel got to where it is

**Purpose.** A retrospective architecture map of the reTerminal **D1001** wall-panel program: the phases we
went through, the shared modules we created, the decisions (ADRs) that anchored them, and the hard-won
lessons. Written to (a) preserve the reasoning behind the current state and (b) be the springboard for the
**E1001** roadmap ([e1001-roadmap.md](e1001-roadmap.md)) — because most of this work was built to be reused.

Scope: ~73 commits, ADR-0019/0020/0021/0022/0023/0024, four design docs, and ten shared firmware components.
Citations are real commit hashes in `ha-coord`.

---

## The arc, in phases

### Phase 0 — Architecture first (ADR-0019, ADR-0013, ADR-0011)
Before any panel code, the *screen-interface architecture* was designed: panels are **thin MQTT clients that
render a server-authored spec**, never bespoke UIs. The device-intake captured the two target boards up front.
- `e9ee4f6` reterminal intake: firmware-backup procedure + **device facts (D1001/E1001)**
- `d7bd444` **ADR-0019 (Proposed)** screen-interface architecture · `70e310c` SD-presence-gated data-agent ·
  `27202a8` §6 D1001 as BLE edge-relay gateway
- **Result:** the reuse contract — API-first backend, per-client BFF view-models, panels as renderers, a
  **capability-descriptor** model that scopes each device's roles. E1001 was designed *in* from day one.

### Phase 1 — Beachhead: connectivity + OTA before UI
Prove the risky plumbing (P4 ↔ C6 `esp-hosted` radio → WiFi → MQTT → **OTA**) with the display still "hello",
so a bad UI can never brick the device out of reach.
- `988e803` connectivity PROVEN · `9f88b81` **Phase 1 COMPLETE: OTA proven** + remote-debug-over-MQTT tooling
- **Result:** the beachhead-first discipline (borrowed from the Levoit intake) that every later flash relied on.

### Phase 2 — Display + interactive tiles (ADR-0019 Phase 2)
- `eff5435` display bring-up PROVEN (JD9365 MIPI-DSI + LVGL) · `3bd172a` server-backed LVGL renderer ·
  `4d84a3e` MQTT live-state + crash fixes + vendor components in-tree · `913daee` tap-to-detail ·
  `98cb671` **touch → signed device commands** (`93aa2b1` operator-scoped panel token)
- **Result:** a working control panel — LVGL tiles fed by BFF view-models + live MQTT deltas, issuing
  role-gated commands.

### Phase 3 — Panel ⇄ PWA parity via a shared UI spec (ADR-0019 Phase 0/A/B/C)
Make the panel and the web app **two thin renderers of one server-authored spec**, not two divergent UIs.
- `9fe4798` shared-UI-spec seed (METRIC_CATALOG) · `bcabb2a` inline expand + 72h `lv_chart` ·
  `1dc438a` scenes + admin lock + actuator controls · `a1c99ed`/`0de4848` server-authored `vm.controls` ·
  `104411c` panel renders controls from `vm.controls` (v55)
- **Result:** "new device/trait = zero panel change" — UI *decisions* live once, in the BFF.

### Phase 4 — Shared firmware core: the module-first turn (ADR-0020, ADR-0021)
The pivotal architectural move: stop `cp -r`-forking firmware; extract **shared IDF components** with
board-agnostic seams. **A device becomes a column in [MATRIX.md](../../edge/MATRIX.md), not a fork.**
- `e118e23` ADR-0020 plan + ADR-0021 agent-navigation tree · `343c088` **switchbot_decode** (first component) ·
  `d793b77` **ha_ble_scan** + `e5e1e1a` panel adopts it (validated live) · `96707ec` generated MATRIX +
  drift-guard test · `bf39596`/`5f0a55a` edge nodes migrate onto the shared BLE core · `c516da1` extract
  **ha_sdcard / fs_ops / ha_battery** · `af0db77` **accept + institutionalize the module-first mandate**
- **Result:** the shared-component catalog + the "column not fork" model — the single biggest lever for E1001.

### Phase 5 — BLE edge-relay bring-up + C6 reflash (ADR-0019 §6)
The panel becomes an *edge node*, not just a display: the C6 does WiFi **and** BLE (HCI-over-SDIO coexistence),
harvesting room BLE adverts onto the bus.
- `a46fef3` BLE-over-VHCI spike · `bed6006` C6 serial-flash procedure (over-SDIO reflash impossible) ·
  `6693881` **DONE — C6 reflashed to 2.12.9, BLE working** · `9568c60` battery power-cycle gotcha
- **Result:** house-wide distributed BLE coverage from the same C6 that carries WiFi — zero extra hardware.

### Phase 6 — Local data: rollup ladder + SD replica (ADR-0022)
Panels become **local-cache + recovery nodes**: charts render from a local SD copy of a multi-resolution
"rung" database, server-independent.
- `cf2f435` vendor **sqlite3** + FATFS VFS (rung-DB substrate) · `8c3eee6` rollup-ladder engine
  (raw→1min→1hour→1day→1week) · `50ed4e2` serve the ladder (full.db + manifest) · `822bf11` Phase 2
  (parquet backfill + since-NDJSON) · `13f0c23` **ui_tiles 1393→206 L split** + LVGL heap → PSRAM ·
  `417126d` ha_replica seed-pull · `5b9d815` charts query the local SD replica (v57)
- **Result:** offline/instant charts; each panel a redundant data copy below the warm standby.

### Phase 7 — Battery/charge investigation (the "firmware is the problem" arc)
Getting the battery *understood*: fuel-gauge hunt, ADC gauge, and the charge saga that ended in a schematic-
grounded truth — **our firmware was the fault, not the hardware.**
- `b12fd83` i2cscan (hunt the gauge) · `5ee0012` battery indicator via ADC (per Seeed BSP) · `339b6a2`
  charge/gauge fixes + working SD · `5dc6a5d`/`90ca6e7`/`c92a027` schematic-grounded hardware reference ·
  `b35b068` **correct the charge root cause — it's OUR firmware, not the HW** · `2c16c1d` mirror the factory
  charge logic, **kill the self-defeating watchdog**
- **Result:** a trustworthy ADC gauge + charge manager mirroring the proven factory BSP; the discipline that
  the reference implementation must be run verbatim before blaming silicon.

### Phase 8 — Battery power policy + accurate gauge (ADR-0024) — *current*
The most recent, most intensive arc: a user-facing power-off, then a **system-wide battery standard** — a
state-normalized gauge, a safety policy (shutdown/warn/boot-gate), and a re-characterizable profile.
- `80c810e` user-facing power-off (`PWR_HOLD` latch) + SD-absent panic fix · `fca6cc2` **ADR-0024** standard ·
  `3fc54d9` ADC race fix + SD hot-plug + MQTT profiler · `975e3cb` **ha_power_policy** (safety heart) ·
  `0f775e8` power-topology + USB-kill resolution (schematic) · `0d15ae5` **ha_battery_profile** (loadable) ·
  `8e62ec4` characterization harness · `d28fd3e`→`d63aca8` regressed gauge + hardware-100% anchor ·
  `485ca56` anchoring guidance · `1863020` **policy wired + HW-verified** (v58; first 100% in device history)
- **Result:** a battery subsystem that is a config + a characterization run away from reuse on any device.

---

## Shared modules created (the reusable catalog)

Everything here is board-agnostic by design — board differences are `cfg` structs and injected callbacks, so a
new device supplies a preset, not a fork. This is the E1001 dividend.

| Module | Introduced | Purpose | Board-agnostic seam |
|---|---|---|---|
| `switchbot_decode` | `343c088` | pure BLE advert → reading decoder | none (pure); host-tested |
| `ha_ble_scan` | `d793b77` | shared NimBLE observer | `controller_init` cb (native vs esp-hosted VHCI), publish sink, duty-cycle flags |
| `ha_sdcard` | `c516da1` | SDMMC mount + hot-plug watcher | `ha_sdcard_cfg_t` (pins/power/LDO), detect GPIO |
| `fs_ops` | `c516da1` | SD file-ops over MQTT | device-agnostic (topic + publish sink) |
| `ha_battery` | `c516da1` | ADC gauge + thermal-gated charge mgr | `ha_battery_cfg_t` (ADC/divider/expander pins/LUT), `display_on_fn`, injected expander+I2C, `_d1001_cfg()` |
| `ha_reach` | ADR-0023 | mesh reach census | publish sink + node id |
| `sqlite3` | `cf2f435` | vendored SQLite + FATFS VFS | pure vendor + VFS shim |
| `ha_battery_profile` | `0d15ae5` | versioned, loadable V→SoC profile | `ha_batt_profile_t` (offsets/LUT/floors/provenance), `_d1001_default()` |
| `ha_power_policy` | `975e3cb` | battery **safety** policy (shutdown/warn/boot-gate) | `_cfg` (mV thresholds) + 4 injected actuators (`read_mv`/`power_off`/`warn`/`led`) |
| `sgp40`, `sensirion_gas_index` | `bd2aa34` | VOC gas sensing (edge nodes) | I2C bus handle |

Still **forked / inline** on the panel (future extraction): `app_main`, `display` BSP, `ha_mqtt`, `ha_wifi`,
`ha_ota`, `bat_profile`, `gatt_*` — see [MATRIX.md](../../edge/MATRIX.md).

---

## Decision record (the docs the code hangs on)

- **ADR-0019 screen-interface-architecture** — panels render a server-authored spec; capability descriptors;
  the D1001/E1001 profiles; the phased plan (we are through Phase 6 for D1001, Phase 4 = E1001).
- **ADR-0020 shared-edge-panel-firmware-core** — the module-first mandate; device = a MATRIX column.
- **ADR-0021** repo/agent navigation · **ADR-0022** multi-resolution rollup ladder + replica ·
  **ADR-0023** mesh reach census · **ADR-0024 battery-monitoring standard** (gauge + policy + profile).
- Design docs: [battery-power-policy](battery-power-policy.md), [panel-ui-modularization](panel-ui-modularization.md),
  [shared-ui-spec](shared-ui-spec.md), [rollup-ladder-and-replica-sync](rollup-ladder-and-replica-sync.md).
- Hardware truth: `docs/hardware/` (schematic-grounded D1001 reference) + the KiCad schematic.

---

## Hard-won lessons (the meta-results)

1. **Docs-first on any hardware fact.** Guessing D1001 hardware cost hours; the schematic + factory BSP had the
   answer the whole time. A wrong fact asserted from inference is worse than "I need the doc."
2. **Our firmware is the default suspect.** The charge "bug" was ours; the fix was mirroring the factory BSP
   verbatim (`b35b068`, `2c16c1d`). Can't blame silicon until the reference runs verbatim.
3. **Module-first, decompose before dev.** New capability = a new module with a board-agnostic seam, decided
   *before* code (ADR-0020, `af0db77`). This is what makes E1001 mostly-reuse.
4. **Anchor to hardware truth, not proxies.** The gauge reads 100% because the *charger* says done, not because
   a voltage guessed it (ADR-0024 §7). Base-frame anchoring from the unplug transition; offsets are SoC-
   dependent. Normalize so a state change never jumps the reading.
5. **Beachhead-first, lifeline-always.** Prove connectivity+OTA before UI; never let a subsystem (display, SD,
   battery) knock the device off the bus. Every feature is non-fatal.

---

## Where the D1001 stands now (results)

A mains-capable, always-on control panel that is simultaneously: a **server-spec renderer** (tiles/charts/
controls/scenes, panel⇄PWA parity), a **BLE edge-relay gateway**, a **local-cache + recovery node** (SD rung
replica), and a **battery-managed device** with an accurate state-normalized gauge, a safety policy
(shutdown/warn/boot-gate), and a user-facing power-off — all built on a shared-component catalog engineered
for the next board. That catalog is the subject of the [E1001 roadmap](e1001-roadmap.md).
