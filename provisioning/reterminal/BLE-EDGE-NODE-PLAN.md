# D1001 as a room BLE edge node — implementation runbook (ADR-0019 Phase 6, §6)

**Status:** PLANNED 2026-07-01, approved by Hugh. Implement next session (fresh start after the panel
Phases A/B/C + auto-boot + strobe fix all shipped & HW-verified on `v25-actuator`).

## Goal
Make the always-on D1001 double as a **room BLE edge node**: the P4 harvests BLE sensor advertisements in
its room and relays them onto the canonical HA bus (`home/<area>/<id>/state`), exactly like the existing
edge relays — turning the panel fleet into distributed BLE coverage (ADR-0015). The panel keeps being a
control surface; the BLE work runs on the P4's spare core.

## The architecture (why this is the right shape)
- The C6 is **the P4's only radio** (WiFi + BLE + 802.15.4), reached via `esp-hosted` over SDIO. It runs
  Espressif's **NCP slave** firmware — a network co-processor, NOT a standalone node. So the D1001's C6
  **cannot** run the deployed C6 edge node's *application* firmware; that would cut the panel off the net.
- Instead: run the edge-node **logic on the P4**, using the C6 as the BLE radio via **HCI-over-SDIO**. The
  P4 (32 MB PSRAM, dual RISC-V, a spare core) is a far better host than a bare C6.
- **De-risked already:** the Phase-1 boot log showed the factory C6 slave advertising `WLAN` +
  `HCI over SDIO` + `BLE` (ADR-0019 §6). So the controller is exposed; we still must prove the P4-side
  NimBLE host actually scans through it.

## Reuse (the deployed edge firmware is modular — port, don't reinvent)
Source: [`edge/esp32c6/`](../../edge/esp32c6/). Directly portable to the P4:
- **`main/switchbot_decode.c/.h`** — PURE advert decode (no hardware deps). Lift verbatim + its test
  (`test/test_switchbot_decode.c`).
- **`main/ble_scan.c/.h`** — NimBLE **observer** (passive scan) loop; the config is standard NimBLE
  (`BT_NIMBLE_ROLE_OBSERVER`), which on the P4 binds to the hosted HCI transport instead of a native
  controller. This is the piece that must be re-pointed at esp-hosted.
- **MQTT publish** — reuse the panel's already-live MQTT client (`beachhead_main.c`), publishing canonical
  `home/<area>/<id>/state`. (The edge node's `ha_mqtt.c` is the reference for the payload shape.)
- **Later:** `gatt_history.c` / `gatt_exec.c` (NimBLE central — active GATT history pull). More complex;
  Stage 2.

## Plan (staged; each independently OTA-able on the live panel, rollback-safe)

### Spike 0 — STATUS: firmware BUILT & config-proven (2026-07-01), HW-verify pending
**The feasibility question is answered at the build/config level.** esp_hosted 2.12.9 ships
`examples/host_nimble_bleprph_host_only_vhci` — a NimBLE **host-only** stack on the P4 with the
controller on the co-processor over **VHCI**. That is exactly our shape. The decisive facts:
- IDF v5.4 `components/bt/Kconfig`: `config BT_ENABLED depends on !APP_NO_BLOBS` (NOT `SOC_BT_SUPPORTED`)
  → the host-only NimBLE stack **builds on the P4** even though the P4 has no native BT radio.
- esp_hosted Kconfig exposes `ESP_HOSTED_ENABLE_BT_NIMBLE` + `ESP_HOSTED_NIMBLE_HCI_VHCI`, and the host
  API exports `esp_hosted_bt_controller_init()/enable()` (host/api/src/esp_hosted_api.c).
- **The exact sdkconfig recipe (lifted from the example, now in `beachhead/sdkconfig.defaults`):**
  ```
  CONFIG_BT_ENABLED=y
  CONFIG_BT_CONTROLLER_DISABLED=y          # P4 has no controller; it lives on the C6
  CONFIG_BT_BLUEDROID_ENABLED=n
  CONFIG_BT_NIMBLE_ENABLED=y
  CONFIG_BT_NIMBLE_TRANSPORT_UART=n
  CONFIG_ESP_HOSTED_ENABLE_BT_NIMBLE=y
  CONFIG_ESP_HOSTED_NIMBLE_HCI_VHCI=y
  CONFIG_FREERTOS_HZ=1000                   # example sets 1kHz for VHCI/controller timing
  ```
  ⚠ These land ONLY via a full `sdkconfig` regen from defaults — a pre-existing `sdkconfig` with
  `# CONFIG_BT_ENABLED is not set` overrides the defaults file. Delete `sdkconfig` + `idf.py reconfigure`
  (verified the only delta vs the v25 config is BT/NimBLE/COEX/tick — no other reverts).
- ⚠ Once `main/CMakeLists.txt` declares any `REQUIRES`, `main` loses its implicit "depends on every
  component" — so all deps must be enumerated (done; `bt` + `esp_hosted` are the new ones).
- **Firmware:** `v26-blespike` builds clean (1.61 MB, 62% free). Host init sequence (in `main/ble_spike.c`):
  `esp_hosted_bt_controller_init()` → `_enable()` → `nimble_port_init()` → host task → on-sync passive
  **observer** `ble_gap_disc()`. Every advert bumps counters; telemetry on `d1001-beachhead/ble`.
  MQTT-gated (`cmd/ble on`) + non-fatal so a bad BLE bring-up can't knock the panel/OTA off the bus.

**REMAINING (HW-verify — the actual PASS/FAIL gate):** OTA `v26-blespike` to `.8`, `cmd/ble on`, watch
`d1001-beachhead/ble` — PASS = `adv_total` climbs + `uniq_macs` > 0 (adverts route host↔slave over VHCI).
Also confirm the 1 kHz tick didn't disturb the display and WiFi/MQTT stay `rc:0` under BLE load (the
`esp_hosted` 2.12↔2.3 mismatch stress-test). Held for Hugh to drive (flashing a live panel + BLE sensors
are in his space; tick change could disturb the display non-recoverably since MQTT-connect self-marks-valid).

### Spike 0 HW result (2026-07-01): host PROVEN, factory C6 slave was the wall → fixed via C6 reflash
OTA'd `v26-blespike` to `.8`; `cmd/ble on` → **`bt_controller_init failed 0x106` (ESP_ERR_NOT_SUPPORTED)**.
Traced: `esp_hosted_bt_controller_init` → `rpc_bt_controller_init` sends a `FEATURE_BT/BT_INIT`
feature-control RPC to the slave and returns the slave's answer. A clean reject in **6 ms** (not a timeout)
= **the factory C6 NCP firmware (esp_hosted 2.3.0) doesn't share BT** (its `ESP_HOSTED_CP_BT` was off /
too old). The P4 host side was 100% correct — NimBLE-over-VHCI built, inited, and asked the controller to
come up. Display + WiFi survived the 1 kHz tick change (`display:true`, `wifi_rc:0`, `mqtt_rc:0`). NOT an
antenna issue — `running:false` means we never reached the radio (antenna would be `running:true`+0 adverts).

**The fix (chosen: build+flash now) — matched 2.12.9 C6 slave with BT sharing, flashed over SDIO:**
- Built the slave from the vendored `managed_components/espressif__esp_hosted/slave/` project (copied to
  off-git `~/reterminal-dev/c6-slave/`): `idf.py set-target esp32c6 && idf.py build`. Stock C6 defaults
  already give **`ESP_HOSTED_CP_BT=y` + `CP_WIFI=y` + `BT_CONTROLLER_ONLY=y` + `BT_LE_HCI_INTERFACE_USE_RAM=y`**
  + SDIO transport on C6 pins 18–23 (the Espressif reference wiring Seeed's factory slave was built from →
  matched by construction). Artifact: `build/network_adapter.bin` (~1.13 MB, app-only). This also lands a
  **matched 2.12.9 host+slave**, retiring the 2.12↔2.3 mismatch.
- Flash mechanism = **host-driven slave-OTA over SDIO** (no rewiring, no physical C6 access). The URL one-call
  `esp_hosted_slave_ota()` is only a deprecated decl (moved to an unvendored example); the linked API is the
  low-level `esp_hosted_slave_ota_begin/write/end/activate`. Panel firmware **`v27-slaveota`** adds
  `cmd/slaveota <url>`: streams the bin via `esp_http_client` in 4 KB chunks → writes to the C6's inactive
  OTA slot → activates (C6 reboots). Uses the C6's OWN partition scheme, so a layout mismatch errors rather
  than bricks; result on `d1001-beachhead/slaveota`.
- **Deploy:** serve `network_adapter.bin` on `:8090`; `mosquitto_pub .../cmd/slaveota <url>`; then re-run
  `cmd/ble on` — PASS = `running:true` + `adv_total` climbs. Recovery if the C6 link regresses: physical C6
  UART reflash (bench/USB access on hand).

**✅ 2026-07-01 pm — DONE + HW-VERIFIED. C6 serially reflashed to 2.12.9 (`CP_BT=y`); all gates pass.**
Hugh exposed the `ESP32_C6_Debug` header (TXD/RXD/GND wired; BOOT/RST = pins, jumpered to GND). Sequence that
worked: (1) `sudo chmod 666 /dev/ttyACM0 /dev/ttyUSB0` (not in `dialout`); (2) park P4 in ROM bootloader
(`esptool -p /dev/ttyACM0 --before default_reset --after no_reset --no-stub chip_id`); (3) manual C6 download
mode = hold BOOT→GND, tap RST→GND, release BOOT; (4) probe `esptool -p /dev/ttyUSB0 --chip esp32c6 --before
no_reset --after no_reset chip_id` → **ESP32-C6FH4 rev v0.2, 4 MB**; (5) **factory backup** 4 MB →
`~/c6-factory-2.3.0-backup.bin` (99 s, held stable); (6) `write_flash` full image (bootloader+parttable+otadata+
`network_adapter.bin`) → *Hash verified*; (7) power-cycle. Gates: **① WiFi/slave healthy** (`v27` `wifi_rc:0`);
**② BLE WORKS** — `cmd/ble on` → `running:true`, `adv_total` 99→115 climbing, **5 unique MACs** (RSSI −95..−100 =
bare internal antenna, no SMA — coverage item, not functional); **③ P4 WiFi-OTA** proven (v26→v27 earlier);
**④ over-SDIO C6 reflash NOW WORKS** — `cmd/slaveota` → `begin→progress→complete err:0x0` (the finalize that
was `0x106` on 2.3.0), C6 rebooted into the OTA'd slot and recovered clean (`wifi_rc:0`). **⇒ future C6 updates
are wireless — the programmer rig is no longer needed.** `esptool` buffering gotcha: pipe `mosquitto_sub` through
`stdbuf -oL` and use `-C <n>` for clean-exit capture (raw `timeout` kills before flush). **NEXT = Stage 1.**

**⛔ 2026-07-01 (earlier) — over-SDIO reflash was IMPOSSIBLE on the FACTORY slave.** `cmd/slaveota` (on v27) transferred
all 1,156,208 bytes (begin + every write OK) then **failed `0x106` at finalize** (`ota_end`/`activate`); C6
never rebooted, WiFi stayed `rc:0`. Root cause (migration_guide): **slave-OTA requires slave > 2.5.X; the
factory C6 is 2.3.0**, which predates the OTA-finalize RPC. There is no over-the-wire shortcut — the C6 must
be flashed **serially over UART once**. Hugh has a programmer + pogo pins (hardware access OK). **→ Full
step-by-step: [C6-SLAVE-FLASH-PROCEDURE.md](C6-SLAVE-FLASH-PROCEDURE.md).** After that one serial flash the
slave is 2.12.9 (matched) and future updates CAN use `cmd/slaveota`. Firmware ladder now on `.8`:
`v27-slaveota` (adds `cmd/slaveota`); the panel is fully functional, just BLE-blocked until the C6 flash.

### Spike 0 (original) — BLE-over-hosted feasibility (DO FIRST; cheap, decisive)
On the panel beachhead (`~/reterminal-dev/d1001-beachhead`), enable NimBLE host on the P4 + the esp-hosted
BLE transport, register a passive **observer** scan, and log any adverts received.
- **Config:** `esp_hosted` BLE feature on; NimBLE host (`CONFIG_BT_ENABLED`, `CONFIG_BT_NIMBLE_ENABLED`,
  `CONFIG_BT_NIMBLE_ROLE_OBSERVER`) with the **controller = hosted HCI** (not a native P4 controller — the
  P4 has none). Confirm the esp-hosted slave HCI channel binds (the boot log already advertises it).
- **Pass:** any adverts arrive over MQTT debug → green light, C6 untouched.
- **Fail:** the factory slave doesn't actually route HCI, or NimBLE can't bind → decision point: reflash the
  C6 to a BLE-capable hosted slave (which ALSO resolves the 2.12↔2.3 `esp_hosted` mismatch the "up" way).
  Needs C6 flash access (its own UART/USB path or a P4-mediated passthrough).
- Keep it **command-triggered + non-fatal**, mirroring the display bring-up discipline: BLE bring-up must
  never knock the panel/OTA lifeline off the bus.

### Stage 1 — passive relay MVP (the useful cut)
Port `switchbot_decode.c` + the observer loop into the panel firmware; on each decoded advert, publish
`home/<area>/<id>/state` via the live MQTT client. Passive only (no connections). Now the panel is a control
surface **and** ingests its own room's BLE sensors — one node, both jobs. Fold into ADR-0015 edge-relay
coverage (MAC→device via the existing `edge_mapper`).

### Stage 2 — GATT history pull (later)
Port `gatt_history`/`gatt_exec` (NimBLE central, active connections via the C6) for gap-filling history.
Heavier; defer until Stage 1 is stable.

## Risks / watch items
- **WiFi + BLE share the single C6 radio AND the SDIO link.** BLE scanning adds RPC traffic — this is
  exactly where the **`esp_hosted` 2.12↔2.3 host/slave mismatch** could finally bite (it's been flawless
  for WiFi-only all of the panel work; `wifi_rc:0/mqtt_rc:0`). Spike 0 must stress-test link stability with
  BLE active. If it degrades, the C6-slave update moves from "optional" to "the fix." (Hugh chose "leave
  the mismatch" while WiFi-only was stable; BLE is the trigger to revisit.)
- **Coexistence** is time-domain (passive scan + light MQTT is fine; not heavy simultaneous throughput).
- **Antenna:** external SMA strongly recommended for the gateway role — the D1001's metal enclosure + LCD
  compromise the internal antenna (ADR-0019 §6).
- Keep the BLE task on the P4 spare core; never block the UI / MQTT-callback / touch stacks (the recurring
  queue+worker lesson from the panel work).

## Resume pointer
Start with **Spike 0** on the beachhead. Firmware base = `v25-actuator` (device `.8`, ota_1, committed
`1dc438a`). The panel's WiFi/MQTT/OTA/LVGL/controls are all live; BLE slots onto the spare core. Board item:
`ble-edge-node`.
