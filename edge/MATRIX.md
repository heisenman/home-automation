# MATRIX.md — device × module matrix

Which module each device build links. Pairs with [MODULES.md](MODULES.md).

> **Maintenance:** today hand-maintained (modules are `cp -r` forks). Under ADR-0020 this table is
> **generated from each device's `CMakeLists REQUIRES`** and CI-checked, so it can't drift. A new device is a
> **new column**, not a fork.

| Module | esp32c3 | esp32c6 | esp32s3-eth | **D1001 panel** | E1001? | non-Seeed? |
|--------|:-------:|:-------:|:-----------:|:---------------:|:------:|:----------:|
| `switchbot_decode` | ✓ (fork) | ✓ (fork) | ✓ (fork) | ✓ **shared** | ✓ | ✓ |
| `ble_scan` → `ha_ble_scan` | native (fork) | native (fork) | native (fork) | **shared, VHCI** ✓ | ? | ? |
| `gatt_exec`/`gatt_history` | ✓ | ✓ | ✓ | ⟶ Stage 2 | … | … |
| `ha_mqtt` | ✓ | ✓ | ✓ | ✓ (panel has its own client) | … | … |
| `ha_relay` | ✓ | ✓ | ✓ | ⟶ (peer node) | … | … |
| `ha_config` | ✓ | ✓ | ✓ | ✓ | … | … |
| `ha_wifi` | ✓ | ✓ | ✓ | esp-hosted-WiFi | … | … |
| `ha_eth` | — | — | ✓ (W5500) | — | — | ? |
| `ha_sntp` | ✓ | ✓ | ✓ | ✓ | … | … |
| `ha_ota` | ✓ | ✓ | ✓ | ✓ (+ `cmd/slaveota` for the C6) | … | … |
| `ha_led` | — | — | ✓ (WS2812) | — | ? | ? |
| `app_main` | ✓ | ✓ | ✓ | panel app (display+control+BLE) | … | … |
| display (LVGL) | — | — | — | ✓ (ADR-0019) | ✓ | ? |
| `ha_battery` *(planned)* | — | — | — | ✓ (needs fuel-gauge ID) | ✓ | ? |

**Panel note:** the D1001 runs the app on the **P4**; its C6 is a dumb NCP radio (esp-hosted). BLE goes
P4 → VHCI → C6. C6 slave = matched esp_hosted **2.12.9** (`CP_BT=y`), serially flashed once; future C6 updates
are wireless (`cmd/slaveota`). So the panel is a **constrained** peer edge node (SDIO-shared radio, weaker
antenna) — same modules, tighter coexistence.
