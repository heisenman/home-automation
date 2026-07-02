# ha_ble_scan

**Role:** shared passive BLE observer. NimBLE host → continuous passive scan → advert parse
→ `switchbot_decode` → per-MAC debounce → platform sink callback. One canonical scanner for
every node **and** the D1001 panel (ADR-0020, ADR-0001).

**Contract:** `include/ha_ble_scan.h`.
- `ha_ble_scan_start(cfg)` — start the observer on its own NimBLE host task. `cfg` supplies the
  two platform seams:
  - `controller_init` — bring up the BLE controller before `nimble_port_init()`. `NULL` for
    native (edge c3/c6/s3); the panel passes `esp_hosted_bt_controller_init()`+`_enable()` (VHCI).
  - `on_reading(mac_str, r, rssi, user)` — fired once per fresh (post-dedup) decoded reading.
    Edge → `ha_relay_allowed` gate + `ha_mqtt_publish_reading`; panel → canonical
    `home/edge/<node>/<mac>/adv` publish.
- `ha_ble_scan_pause/resume` — free the single radio for a GATT pull (Stage 2).
- `ha_ble_scan_running`, `ha_ble_scan_stats` — observability.
- `ha_ble_own_addr_type`, `ha_ble_lookup_addr` — for `ble_gap_connect` / GATT (Stage 2).

**Platform support:** native-radio **or** esp-hosted-VHCI. `REQUIRES bt switchbot_decode` — no
transport/MQTT/relay deps (those live in the caller's sink).

**Debounce:** republish on Δtemp>0.1 °C / Δhum≥1 / Δbatt≥1, else at most every 30 s (48-MAC cache).

**Provenance:** parse + decode + dedup are verbatim from the byte-identical `edge/{esp32c3,esp32c6}/main/ble_scan.c`;
the `controller_init` hook generalises the D1001 Spike-0 (`ble_spike.c`) VHCI bring-up. `edge/esp32s3-eth`
had drifted and is reconciled onto this during its gated migration. **Not yet consumed by the live edge
nodes** — the panel adopts it first (ADR-0020 Stage 1); edge migrates gated.
