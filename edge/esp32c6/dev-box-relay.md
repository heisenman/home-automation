# Pointing an ESP32-C6 at the dev box (ha-dev, 192.168.0.210)

**Goal:** relay BLE meter readings to **ha-dev** over Wi-Fi so the meters its onboard
radio can't reach show up, and so the box doesn't depend on its (known-risk) onboard
MediaTek BT. This is the same edge-relay path documented in `README.md` / `SCOPE.md`,
just aimed at the dev box's broker instead of `.245`.

> **Status (2026-06-24):** PLANNED, not executed. The existing board **`c6-bench` is
> a live relay feeding the production `.245` system — do NOT repoint it while `.245`
> is the dictator** (standing rule: don't disrupt `.245`). Use **a spare/second C6**,
> or repoint `c6-bench` **only as part of the `.245`→ha-dev cutover**, when `.245`'s
> edge intake is being retired anyway.

---

## Box side — already done, needs NO changes

Verified on ha-dev 2026-06-24; a C6 is effectively plug-and-play here:

- Broker is **LAN-reachable**: `mosquitto` listens on `0.0.0.0:1883` (not localhost),
  `allow_anonymous true` — a C6 on the Wi-Fi can publish to `192.168.0.210:1883`.
- Edge ingest is **running**: `ha-edge-mapper` (subscribes `home/edge/+/+/adv`,
  republishes `home/<area>/<device>/state` by resolving MAC→id via the registry) and
  `ha-edge-history` are both `active`.
- The **real `instance/devices.yaml` is loaded**, so the mapper can resolve relayed
  MACs to the right device_id/area immediately.

Re-verify any time:
```bash
ss -lntp | grep ':1883'                       # expect 0.0.0.0:1883
systemctl is-active ha-edge-mapper ha-edge-history
mosquitto_sub -h localhost -t 'home/edge/#' -v # watch for incoming relays
```

## One open box-side prerequisite — NTP

Edge readings are timestamped on the node; `SCOPE.md` syncs the C6's clock from the
**dictator's** NTP. ha-dev isn't an NTP server yet. Before deploying a C6 here, either:
- run an NTP service on ha-dev and set the C6's `ntp_server` to `192.168.0.210`, or
- point the C6 at a LAN/internet NTP while online (simplest for dev).

---

## C6 side — the actual work (needs the board on USB + ESP-IDF v5.x)

Per `README.md`, the broker/Wi-Fi/node config is either compiled in (`secrets.h`) or
flashed as runtime NVS. To aim a C6 at ha-dev:

1. **Config** — set broker to the dev box and pick a node id for where it'll sit:
   ```bash
   cd edge/esp32c6
   cp main/secrets.example.h main/secrets.h     # edit: Wi-Fi SSID/pass,
                                                 #   broker IP = 192.168.0.210,
                                                 #   node id   = e.g. c6-c-office
   ```
   (Or the NVS path from README §runtime-config: set `broker_uri`, `node_id`,
   `ntp_server` with `nvs_partition_gen.py` → `idf.py partition flash` — avoids a
   recompile and keeps secrets out of the firmware image.)
2. **Flash** over the C6's built-in USB from the desktop it's plugged into:
   ```bash
   idf.py set-target esp32c6 && idf.py build && idf.py flash monitor
   ```
3. **Place** it near the dead-zone meters — current gaps on ha-dev's onboard radio are
   **`meter_pro_c_office`, `meter_h_bath`, `meter_c_bed`, `aranet_radon` (crawlspace)**.
   Validate on the bench in range first, then move it to its spot.

## Verify the full path (C6 → broker → mapper → writer → dashboard)

On ha-dev:
```bash
mosquitto_sub -h localhost -t 'home/edge/#' -v        # 1. raw relays arriving:
                                                      #    home/edge/<node>/<mac>/adv
mosquitto_sub -h localhost -t 'home/+/+/state' -v     # 2. mapped to canonical state
curl -s localhost:8123/devices | python3 -c \
  'import sys,json;print(len(json.load(sys.stdin)),"devices")'   # 3. count rises past 7
```
The previously-missing meters should now appear in `/devices`, `/api/v1/sensors`, and
the dashboard — labelled by the registry, no per-device UI work (onboarding doc §4).

## Relay payload contract (what the C6 must emit)

Option B "dumb relay, server maps" (the recommended path; mapper does MAC→identity):
```
topic:   home/edge/<node>/<mac>/adv
payload: {"schema":1,"transport":"ble-adv","metrics":{"temperature_c":..,"humidity_pct":..},
          "meta":{"rssi":-78,"mac":"B0:E9:FE:54:..","node":"c6-c-office"}}
```
This mirrors exactly what `server/ingest/scanner.py` emits locally, so the mapper and
writer treat onboard-scanned and C6-relayed readings identically.
