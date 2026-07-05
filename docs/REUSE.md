# REUSE.md — the capability catalog (look here before you build)

**Principle 1 (reuse-first, [ADR-0025](adr/ADR-0025-reuse-first-navigation.md)).** This is the *by-capability*
index — "I need capability X, what already exists?" The *by-location* view is the [`AGENTS.md`](../AGENTS.md)
tree. Before building anything, scan this file + the ADR nearest your task; **reuse or justify in your commit.**

The shared-firmware-module table below is **generated** from each component's header breadcrumb by
`tools/gen_reuse.py` — do not hand-edit it (a hand-kept index rots; `tests/test_agents_nav.py` fails on drift).
The *other reusable surfaces* section is hand-maintained pointers to reuse that isn't a firmware component.

## Shared firmware modules (`firmware/components/`)

<!-- GENERATED:reuse (tools/gen_reuse.py --write) — do not edit by hand -->
| Module | Reuse when | Contract | Header |
|---|---|---|---|
| `fs_ops` | you must read or write a device's microSD live, no reflash (e.g. pull a log/CSV off the card) | beachhead cmd/fs | [firmware/components/fs_ops/include/fs_ops.h](../firmware/components/fs_ops/include/fs_ops.h) |
| `ha_audio` | a board with an ES8311 codec → Class-D amp → speaker needs to emit local alert tones. | ADR-0020 | [firmware/components/ha_audio/include/ha_audio.h](../firmware/components/ha_audio/include/ha_audio.h) |
| `ha_battery` | reading battery voltage/SoC/charge-state on a panel-class ESP node with no fuel-gauge IC | ADR-0020/0024 | [firmware/components/ha_battery/include/ha_battery.h](../firmware/components/ha_battery/include/ha_battery.h) |
| `ha_battery_profile` | a device needs its battery curve/offsets/safety-floors as loadable, testable, deployable data (no reflash) | ADR-0024 | [firmware/components/ha_battery_profile/include/ha_battery_profile.h](../firmware/components/ha_battery_profile/include/ha_battery_profile.h) |
| `ha_ble_scan` | an edge node needs to observe/relay BLE adverts (SwitchBot meters etc.) | ADR-0020 | [firmware/components/ha_ble_scan/include/ha_ble_scan.h](../firmware/components/ha_ble_scan/include/ha_ble_scan.h) |
| `ha_cmd` | any device that ACTS on an authority directive (edge nodes, the D1001 panel) must verify the signed {p,s} envelope before acting, instead of reinventing the scheme. | ADR-0010 | [firmware/components/ha_cmd/include/ha_cmd.h](../firmware/components/ha_cmd/include/ha_cmd.h) |
| `ha_gatt` | any NimBLE-host node (edge c3/c6/s3 native controller, OR the D1001 panel over the esp_hosted HCI) needs to CONNECT to a SwitchBot meter and pull its on-device history log. | ADR-0020 | [firmware/components/ha_gatt/include/ha_gatt.h](../firmware/components/ha_gatt/include/ha_gatt.h) |
| `ha_imu` | a node has the on-board 6-axis IMU and needs board temperature (thermal gating), raw accel, or hardware motion/tap detection (presence, tap-to-wake) | ADR-0019 | [firmware/components/ha_imu/include/ha_imu.h](../firmware/components/ha_imu/include/ha_imu.h) |
| `ha_power_policy` | a battery device needs low-power safety behavior that holds even before an accurate SoC curve exists | ADR-0024 | [firmware/components/ha_power_policy/include/ha_power_policy.h](../firmware/components/ha_power_policy/include/ha_power_policy.h) |
| `ha_reach` | a node should report full-neighborhood BLE reachability for coordinator rebalancing, without the advert firehose | ADR-0023 | [firmware/components/ha_reach/include/ha_reach.h](../firmware/components/ha_reach/include/ha_reach.h) |
| `ha_rtc` | a panel needs a hardware wall clock that survives reboots (holdover) and a "is the time trustworthy yet?" signal, off an I2C PCF8563/PCF8564-class RTC | ADR-0019 | [firmware/components/ha_rtc/include/ha_rtc.h](../firmware/components/ha_rtc/include/ha_rtc.h) |
| `ha_sdcard` | a device needs mounted microSD storage that adapts to new boards without forking | ADR-0020 | [firmware/components/ha_sdcard/include/ha_sdcard.h](../firmware/components/ha_sdcard/include/ha_sdcard.h) |
| `switchbot_decode` | decoding SwitchBot meter/sensor adverts on-device or host (shared with server/ingest) | server/ingest port | [firmware/components/switchbot_decode/include/switchbot_decode.h](../firmware/components/switchbot_decode/include/switchbot_decode.h) |
<!-- /GENERATED:reuse -->

*Vendored third-party (upstream, not our modules — do not breadcrumb):* `sensirion_gas_index`, `sgp30`,
`sgp40`, `sqlite3`.

## Other reusable surfaces (by capability)

Hand-maintained — these are reuse points that aren't a single firmware component. Add here when you build
something cross-cutting a future agent would otherwise rebuild.

- **Read/write a device's SD card live (no reflash)** → the `cmd/fs` handler (`fs_ops`) + `tools/d1001_fs_pull.py`.
  *This is the one this session nearly reinvented — the whole reflash-free profile pull already existed here.*
- **Deploy a battery curve as data (no reflash)** → `cmd/profile` (`ha_battery_profile_rt`) + `tools/d1001_profile_push.py` (ADR-0024 §5).
- **Edge event / advert contracts** → `home/edge/<node>/{event,adv}`; the coordinator's `edge_mapper` maps
  MAC → canonical `home/<area>/<id>/state`. See [edge/AGENTS.md](../edge/AGENTS.md).
- **Server = single UI-truth** → the BFF view-model (`vm.controls`, `METRIC_CATALOG`); PWA + D1001 panel both
  render it (don't add a panel-specific endpoint). See [server/AGENTS.md](../server/AGENTS.md).
- **A code-backed doc that can't rot** → generate it + drift-test it, mirroring `tools/gen_module_matrix.py`
  → `edge/MATRIX.md` (guarded by `tests/test_module_matrix.py`). This file follows the same pattern.
- **Panel/edge tooling** → `tools/` (edge_ota/sign, node_bringup, enroll_node, the `agents/coord.py` board);
  battery profiling: `tools/e1001_*.py`, `tools/d1001_*.py`.

*(Pass 3, dev: server packages get their own generated table here, sourced from [server/AGENTS.md](../server/AGENTS.md).)*
