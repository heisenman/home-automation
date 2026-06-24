# ESP32-S3-ETH edge node — wired BLE-relay for the dictator (VIP)

A **wired** BLE-scan→relay node for the dictator, addressed via the floating VIP **`192.168.0.200`** so it
follows whichever server holds the dictator role across a failover (ADR-0015 Phase 0). Passive-scans
SwitchBot (and other) BLE adverts and relays them over **Ethernet** to the dictator's MQTT broker, so the dead-zone
meters 210's onboard radio reaches only marginally (crawlspace Aranet ~−89 dBm, attic ~−90 dBm) come
through on a robust wire instead of Wi-Fi. Same relay contract as the C6 node:
`home/edge/<node>/<mac>/adv` → `ha-edge-mapper` → `home/<area>/<device>/state`.

## Why a fork of `esp32c6/`, not a shared component
This reuses the C6 firmware's proven modules verbatim — `ble_scan`, `switchbot_decode`, `ha_mqtt`,
`ha_config`, `ha_sntp`, `gatt_*`, `ha_ota` — and swaps **one** thing: the network layer. `ha_wifi.c`
is replaced by **`ha_eth.c`** (W5500 SPI). `app_main.c` calls `ha_eth_connect()` instead of
`ha_wifi_connect()`; everything downstream (SNTP → MQTT → BLE scan) is identical. (A later refactor
could promote the shared files to an ESP-IDF component; kept as a fork for now to avoid disturbing the
live C6 node.)

## Board + wiring — Waveshare ESP32-S3-ETH (W5500)
The ESP32-S3 has **no internal Ethernet MAC**, so the board drives an external **W5500** over SPI.
Pins for the **standard `ESP32-S3-ETH`** (defined in `main/ha_eth.c`):

| W5500 | GPIO |
|-------|------|
| MOSI  | 11 |
| MISO  | 12 |
| SCLK  | 13 |
| CS    | 14 |
| INT   | 10 |
| RST   | 9  |

⚠️ **The industrial `ESP32-S3-ETH-8DI-8DO` / `-8DI-8RO` variants wire the W5500 differently**
(CLK15 / MOSI13 / MISO14 / CS16 / INT12 / RST39). If this board is one of those, edit the six
`#define`s at the top of `ha_eth.c`. Confirmed chip on the bench: **ESP32-S3**, 8 MB PSRAM,
MAC `28:84:85:54:AB:E0`, on `/dev/ttyACM0` (native USB-Serial-JTAG).

## Build + flash
ESP-IDF **v5.4** is installed at `~/esp/esp-idf`.
```bash
. ~/esp/esp-idf/export.sh                 # idf.py on PATH
cd edge/esp32s3-eth
cp main/secrets.example.h main/secrets.h  # set HA_BROKER_URI=mqtt://192.168.0.200:1883, HA_NODE_ID
idf.py set-target esp32s3                  # first time only
idf.py build
idf.py -p /dev/ttyACM0 flash monitor       # native USB; visko must be in the dialout group
```

## Config (`main/secrets.h`, gitignored)
- `HA_BROKER_URI` = `mqtt://192.168.0.200:1883` (the dictator).
- `HA_NODE_ID` = e.g. `s3-crawlspace` — appears in the topic + `meta.node`.
- `HA_NTP_SERVER` = `pool.ntp.org` (wired internet; readings are also mapper-stamped on ingest).
- Wi-Fi fields are **unused** on the wired node (kept only because `ha_config_t` carries them).

## Verify on 210
```bash
mosquitto_sub -h localhost -t 'home/edge/s3-crawlspace/#' -v   # raw relays arriving
mosquitto_sub -h localhost -t 'home/+/+/state' -v              # mapped to canonical state
```
The dead-zone meters should appear in `/api/v1/sensors` and the dashboard, labelled by the registry —
no per-device UI work. This is the robust, wired successor to the planned C6 Wi-Fi relay
(`../esp32c6/dev-box-relay.md`).
