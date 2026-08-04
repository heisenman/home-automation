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
| `ha_config` | any node needs its identity/broker/wifi/ntp resolved as compile-time-default-then-NVS-overlay (don't re-implement the NVS overlay + provisioning precedence). | ADR-0020 | [firmware/components/ha_config/include/ha_config.h](../firmware/components/ha_config/include/ha_config.h) |
| `ha_gas` | a node has (or might have) a gas sensor on I2C and you want it sampled and published without the image needing to know which part is soldered. | ADR-0020 | [firmware/components/ha_gas/include/ha_gas.h](../firmware/components/ha_gas/include/ha_gas.h) |
| `ha_gatt` | any NimBLE-host node (edge c3/c6/s3 native controller, OR the D1001 panel over the esp_hosted HCI) needs to CONNECT to a SwitchBot meter and pull its on-device history log. | ADR-0020 | [firmware/components/ha_gatt/include/ha_gatt.h](../firmware/components/ha_gatt/include/ha_gatt.h) |
| `ha_gatt_exec` | a NimBLE-host node (edge c3/c6/s3, or the panel) must run an arbitrary server-composed GATT interaction against a device beyond the fixed SwitchBot history pull that ha_gatt covers. | ADR-0020 | [firmware/components/ha_gatt_exec/include/ha_gatt_exec.h](../firmware/components/ha_gatt_exec/include/ha_gatt_exec.h) |
| `ha_imu` | a node has the on-board 6-axis IMU and needs board temperature (thermal gating), raw accel, or hardware motion/tap detection (presence, tap-to-wake) | ADR-0019 | [firmware/components/ha_imu/include/ha_imu.h](../firmware/components/ha_imu/include/ha_imu.h) |
| `ha_mqtt` | any edge node needs the signed-command/relay/OTA control plane + the canonical home/edge/<node>/* publish topics. Don't re-implement the HMAC verify + anti-replay + dispatch. | ADR-0010, 0023 | [firmware/components/ha_mqtt/include/ha_mqtt.h](../firmware/components/ha_mqtt/include/ha_mqtt.h) |
| `ha_ota` | any node that accepts firmware over the air (edge c3/c6/s3, the D1001 panel) needs the pinned-host + signed-hash + per-node identity gate before an image is allowed to boot. | ADR-0020 | [firmware/components/ha_ota/include/ha_ota.h](../firmware/components/ha_ota/include/ha_ota.h) |
| `ha_power_policy` | a battery device needs low-power safety behavior that holds even before an accurate SoC curve exists | ADR-0024 | [firmware/components/ha_power_policy/include/ha_power_policy.h](../firmware/components/ha_power_policy/include/ha_power_policy.h) |
| `ha_reach` | a node should report full-neighborhood BLE reachability for coordinator rebalancing, without the advert firehose | ADR-0023 | [firmware/components/ha_reach/include/ha_reach.h](../firmware/components/ha_reach/include/ha_reach.h) |
| `ha_relay` | an edge node needs the dictator to narrow which endpoints it relays (Tier-2 coverage), rather than relay-all. | ADR-0015 | [firmware/components/ha_relay/include/ha_relay.h](../firmware/components/ha_relay/include/ha_relay.h) |
| `ha_rtc` | a panel needs a hardware wall clock that survives reboots (holdover) and a "is the time trustworthy yet?" signal, off an I2C PCF8563/PCF8564-class RTC | ADR-0019 | [firmware/components/ha_rtc/include/ha_rtc.h](../firmware/components/ha_rtc/include/ha_rtc.h) |
| `ha_sdcard` | a device needs mounted microSD storage that adapts to new boards without forking | ADR-0020 | [firmware/components/ha_sdcard/include/ha_sdcard.h](../firmware/components/ha_sdcard/include/ha_sdcard.h) |
| `ha_sntp` | any board needs a wall clock kept fresh enough to pass the signed-command freshness window (don't hand-roll SNTP init/re-sync). | ADR-0015 | [firmware/components/ha_sntp/include/ha_sntp.h](../firmware/components/ha_sntp/include/ha_sntp.h) |
| `sgp41` | a node has an SGP4x on its I2C bus — always route through sgp4x_identify() rather than assuming the part, and reach here (not sgp40) when you need the NOx pixel. | ADR-0020 | [firmware/components/sgp41/include/sgp41.h](../firmware/components/sgp41/include/sgp41.h) |
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

### Server packages (`server/`)

Generated from each package's `__init__.py` breadcrumb (ADR-0025 Pass-3), the same pattern as the firmware
table above. Add a package → add its `# BREADCRUMB: server > … / # REUSE-WHEN: …` header (or `tools/gen_reuse.py
--check` fails). Overview: [server/AGENTS.md](../server/AGENTS.md).

<!-- GENERATED:reuse-server (tools/gen_reuse.py --write) — do not edit by hand -->
| Module | Reuse when | Contract | Header |
|---|---|---|---|
| `api` | you need to expose or render device/room/control data to a client — author it once in the BFF view-model, never a panel-specific endpoint | 0013 | [server/api/__init__.py](../server/api/__init__.py) |
| `cluster` | you're doing heartbeat, VIP/dictator sensing, or failover coordination on the server side | 0016,0018 | [server/cluster/__init__.py](../server/cluster/__init__.py) |
| `comms` | you're emitting an event or need a transport-agnostic resource — use the comms layer, not raw MQTT | 0012 | [server/comms/__init__.py](../server/comms/__init__.py) |
| `control` | you're actuating a device or resolving a control policy/scene — go through the trait loop + signed issuer | 0002,0011,0014 | [server/control/__init__.py](../server/control/__init__.py) |
| `grid` | you need to curtail devices on an external signal — extend a ShedSource, don't add a second control path | 0037 | [server/grid/__init__.py](../server/grid/__init__.py) |
| `ingest` | you're turning device telemetry into canonical readings or adding a device family — reuse a bridge + the registry reloader | 0001,0023 | [server/ingest/__init__.py](../server/ingest/__init__.py) |
| `maintenance` | you're renaming/relocating/retiring a device — reuse device_migrate/relocate/placement, never hand-edit a registry | 0016,0022,0026,0027 | [server/maintenance/__init__.py](../server/maintenance/__init__.py) |
| `mesh` | you're assigning edge-relay coverage or reading mesh topology / reach census | 0015,0023 | [server/mesh/__init__.py](../server/mesh/__init__.py) |
| `notify` | you're raising a user-facing alert — go through the alert engine, don't invent a notify path | - | [server/notify/__init__.py](../server/notify/__init__.py) |
| `storage` | you're reading/writing device readings — go through the two-tier writer, don't touch the DBs directly | 0004,0006,0009 | [server/storage/__init__.py](../server/storage/__init__.py) |
| `util` | you need a cross-cutting helper (mqtt creds, registry reload) — reuse before reimplementing | - | [server/util/__init__.py](../server/util/__init__.py) |
| `weather` | you need weather data — use the weather lane | 0008 | [server/weather/__init__.py](../server/weather/__init__.py) |
<!-- /GENERATED:reuse-server -->
