# Edge Node — ESP32-C6 BLE Scanner Relay (Scope / Design)

**Status:** Scoping — decisions pending (see §10). **Date:** 2026-06-20.
**Phase:** edge-node bring-up (Phases 1–4 era; *not* the Phase-8 Wasm split).

## 1. Goal & immediate value

A battery/USB-powered ESP32-C6 that **passively scans SwitchBot BLE advertisements, decodes them,
and publishes readings to the central Mosquitto broker over Wi-Fi** — a remote sensor relay for the
"dictator" server (ADR-0001).

**Immediate payoff:** it fixes the meters the server's own dongle can't reach reliably. The
backfill just proved **attic** (foil subfloor) and likely **c_office** are out of clean BLE range.
A C6 placed near them relays their readings over Wi-Fi instead of fighting distance over BLE — and
captures their *live* stream that the server currently misses.

## 2. Architecture fit (ADR-bound)

| ADR | Constraint on this node |
|---|---|
| 0001 Dictator | Node only **publishes to the broker**; never coordinates peer-to-peer. Server stays authoritative. |
| 0003 Wasm split | **Native C (ESP-IDF) now** (Phase 8 is the Wasm host). Structure the BLE-scan + MQTT relay as the *foundational* layer; keep decode logic modular so it can become a Wasm peripheral module later. |
| 0005 Secure-boot tiers | Sensor relay → **no Secure Boot, no eFuse lock**; preserve USB esptool recovery. Add **signed OTA + per-node credential + central revocation** before fleet scale. |
| 0002 Capability traits | Telemetry only — no traits/actuation. |

## 3. Firmware approach: **ESP-IDF + NimBLE, native C**

- **ESP-IDF** — the C6 (RISC-V, Wi-Fi 6, BLE 5, 802.15.4) is first-class here; matches ADR-0003's
  "foundational C firmware … ESP-IDF" and gives the cleanest path to the later Wasm host. Arduino-C6
  support is younger; **ESPHome** is fast to a blinky but abstracts away our exact decode + topic
  schema and doesn't host WAMR — wrong fit for this roadmap.
- **NimBLE host** (not Bluedroid) — lighter RAM/flash, ideal for a scan-only foundational image.
- **Single-radio coexistence:** the C6 shares one 2.4 GHz radio between Wi-Fi and BLE. Use ESP-IDF
  SW coexistence; passive scan + periodic MQTT publish coexist fine (we're not a hot path).

## 4. The contract it implements (zero server change for Option A)

The server **writer** subscribes `home/+/+/state` (qos 1) and inserts each metric idempotently.
The C6 emits exactly what the server scanner emits today ([scanner.py](../../server/ingest/scanner.py)):

- **Topic:** `home/<area>/<device_id>/state`  (qos 1, retain true)
- **Payload (JSON):**
  ```json
  {
    "schema": 1,
    "device_id": "meter_attic_south_wall",
    "device_type": "switchbot_meter_outdoor",
    "area": "attic",
    "ts": "2026-06-20T01:23:45Z",
    "transport": "ble-adv",
    "metrics": {"temperature_c": 22.7, "humidity_pct": 39, "battery_pct": 100},
    "meta": {"rssi": -78, "mac": "AA:BB:CC:00:00:01", "node": "c6-attic"}
  }
  ```
- **Decode to replicate** (port [decoders/switchbot.py](../../server/ingest/decoders/switchbot.py) to C):
  - Detect by **manufacturer company 0x0969** OR **service-data UUID 0xFD3D**.
  - **Battery** = `service_data[2] & 0x7F` (NOT the manufacturer status byte — that read >100%).
  - **Format A** (service data ≥6B, Meter/Plus/older Pro): temp = `(b[4]&0x7F)+(b[3]&0x0F)*0.1`,
    sign = `b[4]&0x80`; humidity = `b[5]&0x7F`.
  - **Format B** (manufacturer ≥12B after 6-byte MAC, Outdoor/newer Pro): temp from `b[8..9]`,
    humidity `b[10]&0x7F`.
  - **Validate**: −40..60 °C, 0..100 %RH; drop otherwise.
  - Re-publish thresholds/debounce parity with the server scanner (skip noise).

## 5. KEY DECISION — where the MAC→device registry lives (§10-A)

The topic needs `area`/`device_id`, which come from the registry (real MACs = PII).

- **Option A — edge carries the registry subset.** C6 maps MAC→device_id/area itself and publishes
  the final `home/<area>/<device_id>/state`. **Zero server change.** Cost: registry duplicated to each
  node; a registry edit means re-provisioning nodes.
- **Option B — dumb relay, server maps (recommended).** C6 publishes decoded-by-MAC to e.g.
  `home/edge/<node>/<mac>/adv`; a small server-side mapper resolves MAC→device via the **authoritative**
  registry and feeds the writer. Edges stay dumb/interchangeable (fits ADR-0001 dictator + ADR-0005
  "cheap spares"); registry stays single-sourced on the server. Cost: one small server ingest component.

I lean **B** for architectural cleanliness; **A** is the faster path to a working board.

## 6. Config & secrets (git-ignored, like `instance/`)

Provisioned to **NVS** (or a config partition) at flash time — **never in firmware source or git**:
`wifi_ssid`, `wifi_psk`, `broker_host` (the dictator IP), `broker_port` 1883, `node_id`
(e.g. `c6-attic`), and — if Option A — the MAC→device_id/area table. A `config.example.json` +
a provisioning step go in the repo; the real `config.json` is git-ignored.

## 7. Security posture (sensor tier, ADR-0005)

- **Now:** broker is anonymous on the trusted LAN (matches `mosquitto.conf`). Wi-Fi WPA2/3.
- **Before fleet scale:** per-node MQTT username/credential, **signed OTA**, central revocation,
  MQTT **LWT** (last-will) so the server sees a node drop. No Secure Boot; **no eFuse lock** (keep
  USB recovery). Spoofed-reading blast radius is bounded by cross-node corroboration on the server.

## 8. Reliability details

- **Time/`ts`:** readings need a UTC timestamp. Options: (a) **SNTP** from the dictator (run an NTP
  service on the server — also good air-gapped), or (b) let the **writer stamp `ts` on ingest if
  absent** (simplest; loses sub-second edge accuracy). Decide in §10-B. The server scanner stamps at
  publish today.
- Wi-Fi/MQTT **reconnect with backoff**; buffer a few readings across short broker outages.
- Carry **RSSI** + `node` in `meta` for multi-node de-dup/corroboration on the server.

## 9. Project layout & dev flow

```
edge/esp32c6/
  SCOPE.md                 (this file)
  main/                    ESP-IDF app (ble_scan.c, switchbot_decode.c, mqtt_pub.c, wifi.c, config.c)
  CMakeLists.txt, sdkconfig.defaults
  config.example.json      (no secrets)   |  config.json is git-ignored
  README.md                build + flash + provision steps
```
Dev on the desktop the board is plugged into: ESP-IDF v5.x, **flash over the C6's built-in
USB-Serial-JTAG** (`idf.py flash monitor`). Cheap spares per ADR-0005.

## 10. Decisions — LOCKED (2026-06-20)

- **A. Registry → Option B (dumb relay, server maps).** C6 publishes decoded-by-MAC to
  `home/edge/<node>/<mac>/adv`; a new **server-side edge mapper** resolves MAC→device_id/area via the
  authoritative registry and feeds the writer. Edges stay dumb/interchangeable.
- **B. Timestamps → SNTP from the dictator.** Run an NTP service on the server (.245); the C6 syncs
  and stamps its own `ts`. Also serves the air-gapped future. (Edge `ts` is authoritative; the mapper
  passes it through.)
- **C. Framework → ESP-IDF + NimBLE, native C.** Confirmed.
- **D. First target → bench test first.** Develop on the desktop scanning whatever meters are in
  range, prove the full path (C6 → broker → mapper → writer → dashboard), *then* deploy a board to a
  far meter (attic / c_office / h_bed — the three out-of-range units).

### Still needed from you to build
- **Wi-Fi SSID + PSK** (provisioned into NVS, never committed).
- **NTP on the dictator** — authorize installing/enabling an NTP server on .245 (chrony), or I provide
  copy-paste. Needs sudo there (covered? NOPASSWD is ha-*/mosquitto only — likely a manual step).
- **ESP-IDF on the dev desktop (.112)** — not installed. Installs to `~/esp` (outside `home_automation`)
  and needs `sudo apt` prerequisites → your go-ahead / hands. Confirm broker host = the server IP.

### Dev environment (found)
- Board present on **.112** as `303a:1001` at **`/dev/ttyACM0`** (USB-Serial-JTAG; `idf.py flash monitor`).
- ESP-IDF **not yet installed** on .112.

## 11. Phasing

1. **Bench MVP:** Wi-Fi + SNTP + MQTT + passive SwitchBot scan + decode + publish-by-MAC, with the
   server **edge mapper** + writer ingest. Prove end-to-end into the existing dashboard, on the bench.
2. **Deploy:** move a board to a far meter (attic/c_office/h_bed) and confirm the previously-unreachable
   meter now streams live.
3. Add **Aranet** decode (port `decoders/aranet.py`) — same node covers the crawlspace radon meter.
4. **Hardening:** MQTT LWT (node liveness), per-node credential, signed OTA.
5. **Phase 8:** introduce the WAMR host; move decode into a sandboxed Wasm peripheral module (ADR-0003).

## 12. Build work split (what I can do in-repo now vs. needs you)

**I can build now (all inside `home_automation`, already authorized):**
- ESP-IDF project scaffold under `edge/esp32c6/` (NimBLE scan, SwitchBot decode in C, Wi-Fi, SNTP,
  MQTT publish-by-MAC, NVS config + `config.example.json`).
- The **server edge mapper** (`server/ingest/edge_mapper.py` or a writer extension): subscribe
  `home/edge/+/+/adv`, resolve MAC→registry, republish/insert as `home/<area>/<device_id>/state`
  shape so the existing writer/dashboard need no further change.

**Needs you / outside `home_automation`:** ESP-IDF install (sudo apt + `~/esp`), Wi-Fi creds, NTP on .245.
