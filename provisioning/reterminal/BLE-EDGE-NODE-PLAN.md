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

### Spike 0 — BLE-over-hosted feasibility (DO FIRST; cheap, decisive)
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
