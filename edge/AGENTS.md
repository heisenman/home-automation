# edge/ — ESP32 edge-node firmware

BLE/relay firmware for the edge nodes. Reference builds: `esp32c6/` (WiFi C6), `esp32s3-eth/` (Ethernet/WiFi
S3-POE), `esp32c3/` (WiFi C3 fork). Build/flash + the module map + the hard-won gotchas: **read
[FIRMWARE-GUIDE.md](FIRMWARE-GUIDE.md) first.**

*↑ The by-location node for `edge/` in the [root AGENTS.md](../AGENTS.md) tree (ADR-0021/0025); the by-capability index is [`docs/REUSE.md`](../docs/REUSE.md). Link up, don't duplicate.*

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

## Per-node identity — the cross-provisioning guard (ADR-0020)

An image is branded for exactly **one** node: `main/secrets.h` carries its node id **and** its gas-chip
select (`HA_GAS_SGP30` = SGP30 eCO₂+TVOC, else SGP40 VOC-index). The binding node_id → {mac, sensor, area,
broker, ota_host} lives in the manifest [`esp32c6/nodes.yaml`](esp32c6/nodes.yaml) (loaded by
`../tools/edge_nodes.py`), so `enroll_node.py --from-manifest` emits a complete, board-correct `secrets.h`
with no hand-editing and the flash/OTA identity gate can refuse an image built for a different node. Born
from the 2026-07-05 incident: a `coffice_c6`+SGP40 image was OTA'd onto `cbed_c6` (wrong id + wrong sensor).

**The gate (ADR-0020, three layers):** enroll_node also emits `version.txt` = `<node>@<fw>` → IDF bakes it
into `app_desc.version`. (1) **Node:** the shared `firmware/components/ha_ota` reads the *incoming* image's
`app_desc` and aborts the OTA before boot if its node id ≠ its own (one copy, linked by c6+s3; c3 fork
pending). (2) **Push:** `edge_ota.py` refuses to send a `.bin` whose brand ≠ the target node. (3) **Cable:**
`node_bringup.py` checks the chip's eFuse MAC vs the manifest before flashing.
⚠️ IDF reads `version.txt` only at **configure** time — `node_bringup` runs `idf.py reconfigure` so the brand
lands; a bare incremental `idf.py build` ships an UNBRANDED image that the node gate then (correctly) rejects.

## Gotchas (full list in FIRMWARE-GUIDE §3)

- **BLE+WiFi share one 2.4 GHz radio** → duty-cycle the passive scan on WiFi nodes (~40%) or beacons drop.
- **W5500 eth:** `gpio_install_isr_service(0)` before `esp_eth_start`, or no DHCP (silent).
- **WiFi reconnect unbounded** + down-watchdog reboot; **`esp_netif_init()` exactly once** in `app_main`.
- Signed commands require enrollment (`tools/enroll_node.py`); empty secret rejects all commands.
