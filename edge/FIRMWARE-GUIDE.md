# Edge firmware guide — build a functional node in one shot

The point of this doc: a *new* node (new board, new dev) should reach **boots → relays → OTA-able** without
re-discovering the gotchas that cost us hours on the C6 and S3. Reference implementations:
`edge/esp32c6/` (Wi-Fi C6) and `edge/esp32s3-eth/` (Ethernet/Wi-Fi S3-POE).

The contract a node must satisfy (ADR-0001): **nodes are dumb relays.** A node scans BLE, publishes raw
decoded readings keyed by MAC to `home/edge/<node>/<mac>/adv`; the **dictator owns the registry** and maps
MAC→device/area (`ha-edge-mapper`). Commands come *down* signed on `home/edge/<node>/cmd`.

---

## 1. The module map — what a working node is made of

| Module | Role | Required? |
|--------|------|-----------|
| `ha_config` (+ `secrets.h`) | node id, broker URI, NTP, Wi-Fi creds, **command secret** — compile-time from `secrets.h`, NVS-overridable | **yes** |
| network: `ha_wifi` and/or `ha_eth` | bring up IP. `ha_eth` = W5500 SPI (boards w/ Ethernet); `ha_wifi` = onboard radio | **yes (≥1)** |
| `ha_sntp` | clock sync (best-effort; mapper stamps on ingest anyway) | recommended |
| `ha_mqtt` | broker client: publishes adverts/status/log, **subscribes the signed cmd topic**, verifies `{p,s}` HMAC | **yes** |
| `ble_scan` | NimBLE passive scan → decode → publish; **transport-aware duty cycle** (see §3) | **yes** |
| `switchbot_decode` (+ aranet) | advert byte→reading decoders | **yes** (per device family) |
| `gatt_exec` / `gatt_history` | server-driven GATT (history pulls, actuation) on the shared radio | optional |
| `ha_ota` | signed, host-pinned, image-hash-verified A/B OTA with self-test/rollback | recommended |
| `app_main` | orchestrates: nvs → config → **netif init once** → network → sntp → mqtt → ble_scan → ota-confirm | **yes** |

`app_main` ordering matters: `esp_netif_init()` + `esp_event_loop_create_default()` run **exactly once**
(in `app_main`, not inside the transport drivers — else the second transport's `ESP_ERROR_CHECK` aborts).

## 2. Build & flash workflow

ESP-IDF **v5.4** at `~/esp/esp-idf` (`~/.espressif` toolchains).
```bash
. ~/esp/esp-idf/export.sh                       # idf.py on PATH
cd edge/<node-dir>
# secrets.h: enroll the node (see §5) — do NOT hand-write the command secret
idf.py set-target <esp32s3|esp32c6|…>           # first time; regenerates sdkconfig from sdkconfig.defaults
idf.py build
idf.py -p /dev/ttyACM0 flash monitor            # native USB-Serial-JTAG; user must be in `dialout`
```
`sdkconfig.defaults` carries: target, NimBLE (observer+central), the transport (`CONFIG_ETH_SPI_ETHERNET_W5500`
for W5500 boards), **`CONFIG_ESP_COEX_SW_COEXIST_ENABLE`** when Wi-Fi+BLE coexist, flash size + the A/B OTA
partition table (`partitions.csv`). Forking a node: `cp -r` a reference, swap the transport + `sdkconfig`,
re-enroll for a fresh secret.

## 3. The gotchas that cost real time (read these)

1. **W5500 Ethernet INT:** call `gpio_install_isr_service(0)` **before** `esp_eth_start` — the W5500 driver
   registers an ISR on its INT GPIO; without the service, RX never fires → no DHCP (silent). *(Cost: 1 cycle.)*
2. **BLE + Wi-Fi share ONE 2.4 GHz radio** (S3/C6). A continuous passive scan (`window == itvl`) starves the
   Wi-Fi beacon → `wifi:bcn_timeout` → drops. **Duty-cycle the scan, transport-aware:** full on Ethernet
   (no contention), ~40% on Wi-Fi (`window` 40 ms / `itvl` 100 ms). `app_main` passes `on_wifi` to
   `ha_ble_scan_start()`. Wired nodes pay nothing; Wi-Fi nodes stay associated. *(This is why a Wi-Fi-only
   node needs the duty-cycle to be viable; Ethernet is an upgrade, not a hard dependency.)*
3. **Wi-Fi reconnect must be unbounded:** the original driver capped retries at 20 → permanent offline after a
   flap. Reconnect **forever** on disconnect, plus a **down-watchdog** (`esp_timer`) that reboots after ~2 min
   of no-IP — recovers a wedged stack *and* re-runs auto-sense (so a cable plugged mid-outage is picked up).
4. **Signed commands need enrollment.** `HA_CMD_SECRET ""` (the default) → the firmware **rejects every**
   command, including OTA. You MUST enroll (§5) so the node and the dictator share an HMAC secret.
5. **OTA host pin must be reachable.** `ha_ota.c` pins downloads to `HA_OTA_HOST` (default `192.168.0.245`).
   Set it in `secrets.h` to a host the node can actually route to (e.g. the dictator IP it uses). The signed
   image-hash gate still applies on top.
6. **Address the dictator by the VIP (`192.168.0.200`), not a box** — so the node follows failover. **BUT
   verify VIP reachability *per network segment*:** keepalived's VIP is a secondary IP; some Wi-Fi APs don't
   propagate ARP for it even on the same subnet (observed: `CTWap_24g` reaches `.210` but not `.200`). Wired
   nodes reach the VIP fine; a Wi-Fi node on such an AP must use the box IP until the network is fixed.
7. **A radio is single-tenant:** OTA and GATT pulls **pause** the passive scan (`ha_ble_scan_pause`) so they
   don't fight Wi-Fi/the connection. Don't run two radio consumers at once.

## 4. Networking model
Auto-sense at boot: **try Ethernet first** (short timeout), **fall back to Wi-Fi**. Register the W5500
link-up/down interrupts so a cable plug/unplug **reboots** to re-pick the transport (no polling). Prefer the
VIP for broker/NTP/OTA-host (§3.6). The scan duty-cycle is keyed off which transport won (§3.2).

## 5. Security model — enrollment, signed commands, OTA
Trust root = **physical-presence cable flash from the dictator** (ADR-0010/0011). Per node:
```bash
HA_MASTER_PASSPHRASE="$(cat instance/.master_pass)" python3 tools/enroll_node.py \
    --node-id <id> --mac <chip MAC> --base-secrets <existing secrets.h> --out edge/<node>/main/secrets.h
```
This mints the per-device HMAC secret, records it in the **encrypted LUT** (`instance/node_secrets.enc`), and
writes `secrets.h` (preserving Wi-Fi/broker/NTP from `--base-secrets`). Add `HA_OTA_HOST` after. Then rebuild
+ cable-flash. Commands arrive as `{"p":"<compact json>","s":"<hmac-sha256 hex>"}`; the firmware HMACs the
literal `p` with `HA_CMD_SECRET` and checks freshness (rejects `|dt|>60 s`). OTA = a signed
`{"op":"ota","url":…,"sha256":…}` → host-pin check → download → **partition hash == signed hash** → boot the
inactive A/B slot **pending-verify** → self-test (require MQTT back within ~15 s) → confirm **or auto-rollback**.
Push with `tools/edge_ota.py --node <id> --bin <bin> --serve-ip <reachable> --broker <vip/box>`
(`HA_CMD_SECRET` in env signs it). **Validate OTA on the bench while USB-recoverable, before deploying.**

## 6. LED status / error codes (operability)
Quiet by default; the eye only needs the LED when something's wrong. Drive the onboard **RGB (WS2812)**:
**OFF when healthy**, and on error play a **slow, long, distinguishable** pattern — *color = category, slow
blink-count = code* (humans + eyeballs are slow). Proposed table (impl: `ha_led`, task `led-error-codes`):

| Code | Pattern (slow: ~1 s on / 1 s off, then ~4 s gap, repeat) | Meaning |
|------|----------------------------------------------------------|---------|
| FATAL | **RED** solid | config invalid (no `secrets.h` / no command secret) |
| NET-0 | **RED** × 2 | no network at all (neither Ethernet nor Wi-Fi) |
| WIFI  | **AMBER** × 3 | Wi-Fi link down, reconnecting |
| MQTT  | **BLUE** × 4 | network up but broker unreachable |
| OTA   | **MAGENTA** × 5 | OTA failed / rolled back |
| *(healthy)* | **off** | relaying normally |

The hardwired power LED isn't firmware-controllable (solder-jumper to kill).

## 7. New-node checklist (one-shot)
1. Identify the board → ESP-IDF target; find the transport pins (W5500 SPI / RGB GPIO) from its schematic.
2. `cp -r` the closest reference node; swap the transport module + `sdkconfig.defaults` (target, coex, partitions).
3. **Enroll** (§5) → `secrets.h` (id, broker=**VIP** unless the segment can't reach it, NTP, Wi-Fi, secret); add `HA_OTA_HOST`.
4. Apply the gotchas (§3) for your transport: ISR-before-W5500, duty-cycle on Wi-Fi, unbounded reconnect+watchdog.
5. `idf.py set-target … && build && flash` over USB; confirm `home/edge/<id>/status` = `online` + adverts on the broker.
6. **Validate OTA on the bench** (push a version-bumped image, watch confirm/rollback) *before* it leaves the bench.
7. Place it; verify it relays from its spot (RSSI). Wi-Fi node in a marginal spot → wire it (Ethernet is the cure).
</content>
