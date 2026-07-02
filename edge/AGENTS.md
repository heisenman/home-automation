# edge/ — ESP32 edge-node firmware

BLE/relay firmware for the edge nodes. Reference builds: `esp32c6/` (WiFi C6), `esp32s3-eth/` (Ethernet/WiFi
S3-POE), `esp32c3/` (WiFi C3 fork). Build/flash + the module map + the hard-won gotchas: **read
[FIRMWARE-GUIDE.md](FIRMWARE-GUIDE.md) first.**

## The contract (ADR-0001)

Nodes are **dumb relays.** A node scans BLE, publishes raw decoded readings keyed by MAC to
`home/edge/<node>/<mac>/adv`; the **dictator owns the registry** and maps MAC→device/area (`ha-edge-mapper`).
Commands come *down* **signed** on `home/edge/<node>/cmd` (ADR-0010, per-node `(ts,seq)` anti-replay); ADR-0015
Phase-B coverage directives come down signed+retained on `home/edge/<node>/relay` (`ha_relay`).

## Modules (the catalog)

The module map (`ha_config`, `ha_wifi`/`ha_eth`, `ha_sntp`, `ha_mqtt`, `ble_scan`, `ha_relay`,
`switchbot_decode`, `gatt_exec`/`gatt_history`, `ha_ota`, `app_main`) is documented in
[MODULES.md](MODULES.md); which build links which module is [MATRIX.md](MATRIX.md).

**⚠️ Today these are shared by copy** (`cp -r` forks — `switchbot_decode`/`gatt_*`/`ha_relay` identical across
targets; `ha_mqtt`/`app_main`/`ble_scan` drifted). **ADR-0020** promotes them to real shared IDF components
consumed by the edge nodes **and** the D1001 panel (which is now a BLE peer node via esp-hosted VHCI —
[../provisioning/reterminal/](../provisioning/reterminal/)). Until that lands, port changes to *all* forks.

## Gotchas (full list in FIRMWARE-GUIDE §3)

- **BLE+WiFi share one 2.4 GHz radio** → duty-cycle the passive scan on WiFi nodes (~40%) or beacons drop.
- **W5500 eth:** `gpio_install_isr_service(0)` before `esp_eth_start`, or no DHCP (silent).
- **WiFi reconnect unbounded** + down-watchdog reboot; **`esp_netif_init()` exactly once** in `app_main`.
- Signed commands require enrollment (`tools/enroll_node.py`); empty secret rejects all commands.
