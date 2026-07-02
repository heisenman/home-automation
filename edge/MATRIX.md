# MATRIX.md — device × module matrix

Which module each **real firmware build** links. Pairs with [MODULES.md](MODULES.md).

> **Generated — do not hand-edit the table below.** It is produced from each build's
> `main/CMakeLists.txt` by [`tools/gen_module_matrix.py`](../tools/gen_module_matrix.py) and pinned by
> `tests/test_module_matrix.py` (ADR-0020 drift-guard, same philosophy as `test_viewmodel` pinning the UI
> catalog). Change a build's `SRCS`/`REQUIRES`, then run `python3 tools/gen_module_matrix.py --write`.
>
> **Cell meaning:** `shared` = links the ADR-0020 component (`firmware/components/`, in `REQUIRES`);
> `fork` = still its own `cp -r` source file (in `SRCS`); `—` = not linked by this build. A new device is a
> **new column** (add it to `BUILDS` in the generator), not a fork.

<!-- GENERATED:module-matrix (tools/gen_module_matrix.py --write) — do not edit by hand -->

| Module | esp32c3 | esp32c6 | esp32s3-eth | d1001-panel |
|--------|:-----:|:-----:|:-----:|:-----:|
| `app_main` | fork | fork | fork | fork |
| `ha_config` | fork | fork | fork | — |
| `ha_wifi` | fork | fork | fork | — |
| `ha_eth` | — | — | fork | — |
| `ha_sntp` | fork | fork | fork | — |
| `ha_mqtt` | fork | fork | fork | — |
| `ble_scan` | fork | fork | fork | shared |
| `switchbot_decode` | fork | fork | fork | shared |
| `gatt_history` | fork | fork | fork | — |
| `gatt_exec` | fork | fork | fork | — |
| `ha_ota` | fork | fork | fork | — |
| `ha_led` | — | — | fork | — |
| `ha_relay` | fork | fork | fork | — |
| `display` | — | — | — | fork |

<!-- /GENERATED:module-matrix -->

## Platform / transport notes (hand-written)

- **`ble_scan`/`switchbot_decode` — shared as of ADR-0020 Stage 1.** The panel links the `firmware/components/`
  versions; the edge nodes still link their fork `.c` (gated Stage-2 migration retires them). BLE controller
  differs by platform — **native** on the edge nodes, **esp-hosted VHCI** (P4 → C6) on the panel — but that is
  a `controller_init` callback inside `ha_ble_scan`, not a separate build unit, so it doesn't appear as a row.
- **Panel `—` rows** (`ha_mqtt`/`ha_wifi`/`ha_sntp`/`ha_config`/`ha_ota`/`gatt_*`/`ha_relay`): the panel's MQTT,
  WiFi (esp-hosted), and OTA live **inline in `beachhead_main.c`/`bsp_display.c`** (the `app_main`+`display`
  rows), not yet as shared modules. Folding them in is future refactor work; `gatt_*` arrive with Stage 2, and
  ADR-0015 `ha_relay` coverage with the peer-node migration (today the panel is an unmanaged extra relay).
- **`ha_eth`/`ha_led`** are `esp32s3-eth`-only by nature (W5500 SPI ethernet; WS2812 status LED).
- **Panel radio:** the app runs on the **P4**; the **C6** is a dumb NCP (esp-hosted). C6 slave = matched
  esp_hosted **2.12.9** (`CP_BT=y`), serially flashed once; C6 updates are now wireless (`cmd/slaveota`).
- **Future devices** (E1001, non-Seeed) become **new columns** here once a real build exists — add them to
  `BUILDS` in the generator so the drift-guard covers them from day one.
