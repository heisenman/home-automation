# ESP32-C6 Edge Node — BLE Scanner Relay

Passive SwitchBot BLE scanner that decodes advertisements and publishes them by MAC to the central
Mosquitto broker. The server's **edge mapper** ([../../server/ingest/edge_mapper.py](../../server/ingest/edge_mapper.py))
resolves MAC→device via the authoritative registry and republishes the canonical
`home/<area>/<device_id>/state` the writer already ingests. Design rationale: [SCOPE.md](SCOPE.md).

**Foundational native-C firmware** (ESP-IDF + NimBLE). The Phase-8 Wasm host (ADR-0003) comes later.

## Layout
```
main/app_main.c          boot: nvs → config → wifi → sntp → mqtt → ble scan
main/ha_config.[ch]      config: secrets.h (bench) or NVS (production)
main/ha_wifi.[ch]        STA connect
main/ha_sntp.[ch]        SNTP time + ISO-8601 UTC stamping
main/ha_mqtt.[ch]        esp-mqtt publish + retained LWT (node liveness)
main/ble_scan.[ch]       NimBLE passive scan, AD parse, per-MAC debounce
main/switchbot_decode.[ch]  C port of server/ingest/decoders/switchbot.py
```

## Prerequisites (one-time, needs sudo)
```bash
sudo apt install -y git wget flex bison gperf python3 python3-pip python3-venv \
  cmake ninja-build ccache libffi-dev libssl-dev dfu-util libusb-1.0-0
```
ESP-IDF **v5.4** is already installed at `~/esp/esp-idf` (toolchain in `~/.espressif`).

## Build & flash
```bash
. ~/esp/esp-idf/export.sh                       # puts idf.py on PATH

cd edge/esp32c6
cp main/secrets.example.h main/secrets.h        # then edit: Wi-Fi creds, broker IP, node id
$EDITOR main/secrets.h

idf.py set-target esp32c6                        # first time only
idf.py build
idf.py -p /dev/ttyACM0 flash monitor            # the C6's built-in USB-Serial-JTAG
```

## Provisioning options
- **Bench (default):** `main/secrets.h` (git-ignored). Fast; recompile to change.
- **Production:** leave secrets blank and write NVS (namespace `ha`, keys `wifi_ssid`, `wifi_psk`,
  `broker_uri`, `node_id`, `ntp_server`) with `nvs_partition_gen.py` → `idf.py partition flash`.
  NVS overrides compile-time defaults, so one firmware image serves every node.

## Bench end-to-end test (prove the path before deploying)
On the **server** (.245), make sure the broker, writer, and the **edge mapper** run:
```bash
sudo systemctl restart mosquitto ha-writer
# run the mapper (until ha-edge-mapper.service is installed by install.sh):
venv/bin/python3 -m server.ingest.edge_mapper --registry instance/devices.yaml --log-level DEBUG
```
Watch the traffic and confirm the round-trip:
```bash
mosquitto_sub -h <server-ip> -t 'home/edge/#' -v     # raw edge adv from the C6
mosquitto_sub -h <server-ip> -t 'home/+/+/state' -v  # canonical, after the mapper
```
A meter in range of the C6 should appear on the dashboard within a couple of minutes. Then move the
board to a far meter (attic / c_office / h_bed — the three out of the server's BLE range).

## Published contract
- **Topic:** `home/edge/<node>/<mac>/adv` (qos 1)
- **Liveness:** retained LWT on `home/edge/<node>/status` (`online`/`offline`)
- **Payload:**
  ```json
  {"schema":1,"node":"c6-bench","mac":"AA:BB:CC:00:00:01",
   "device_type":"switchbot_meter_outdoor","ts":"2026-06-20T01:23:45Z","transport":"ble-adv",
   "metrics":{"temperature_c":22.7,"humidity_pct":39,"battery_pct":100},"meta":{"rssi":-78}}
  ```

## Notes
- **Single radio:** Wi-Fi + BLE share 2.4 GHz; SW coexistence is enabled in `sdkconfig.defaults`.
- **Passive scan** (no scan requests) so the node doesn't disturb the meters or other scanners.
- **Debounce:** per-MAC, republish on change (≥0.1 °C / ≥1 %RH / ≥1 % batt) or every 30 s.
