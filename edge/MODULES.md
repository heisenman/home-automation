# MODULES.md — edge/panel firmware module catalog

The tree of firmware modules and what each provides. Pairs with [MATRIX.md](MATRIX.md) (which build links
which). Target = real shared IDF components (ADR-0020); today most are shared by `cp -r` fork.

> Status legend — **shared:** byte-identical across forks (safe to extract first). **drifted:** diverged per
> target (needs reconciliation on extract). **platform:** per-device by nature.

| Module | Role | Contract/ADR | Platform support | Dep notes | State |
|--------|------|--------------|------------------|-----------|-------|
| `switchbot_decode` | Advert bytes → reading (pure, has test) | device-family decode | any | none (pure) | **shared → [firmware/components/](../firmware/components/switchbot_decode/)** — all builds migrated |
| `ha_ble_scan` (was `ble_scan`) | NimBLE passive observer → decode → dedup → sink callback; transport-aware duty cycle (`shared_radio`); optional per-advert `on_sighting` tap (ADR-0023 census, allowlist-independent) | ADR-0001, 0023 | native-radio **or** esp-hosted-VHCI (panel) | NimBLE | **shared → [firmware/components/](../firmware/components/ha_ble_scan/)** — all builds migrated; `on_sighting` is backward-compatible (NULL⇒off, panel unaffected) |
| `ha_reach` | Mesh reach census: per-MAC RSSI-EWMA table fed by `on_sighting`, reported on a server push (`reach/req`) with a long autonomous fallback → `home/edge/<node>/reach`. Decouples observation from actuation (no fossils, organic rebalance) | ADR-0023 | any relay-capable hearer (pure table + callback; no ha_mqtt/ha_relay dep) | none (common IDF) | **shared → [firmware/components/](../firmware/components/ha_reach/)** — c6, s3-eth; panel opt-in pending (ops) |
| `gatt_exec` / `gatt_history` | Server-driven GATT actuation / history pull | ADR-0010, 0020 | native-radio **or** VHCI | NimBLE central | `gatt_exec` → shared [`ha_gatt_exec`](../firmware/components/ha_gatt_exec/) (cfg seams: reply/log; write-gate = compile-time `CONFIG_HA_GATT_ALLOW_WRITE`, default n): **all 3 edge nodes wired + DEPLOYED** (cbed_c6/coffice_c6 OTA, hbed_s3 bench-flash). `gatt_history` → shared [`ha_gatt`](../firmware/components/ha_gatt/): **c3, c6, s3-eth, panel — all wired** (no fork left) |
| `ha_mqtt` | Broker client: adverts/status/log; signed cmd + relay + reach; HMAC verify + anti-replay + OTA/gatt dispatch | ADR-0010, 0023 | any transport | mqtt | **shared → [firmware/components/](../firmware/components/ha_mqtt/)** — c3/c6/s3-eth. Board seam (`ha_mqtt_init`): per-node secrets, LED status hooks (S3), reach flag; gatt/ota/gatt_exec seams wired internally |
| `ha_relay` | Phase-B coverage filter (signed `relay_assign`, NVS, epoch-guarded) | ADR-0015 | any | nvs, json | **shared → [firmware/components/](../firmware/components/ha_relay/)** — c3, c6, s3-eth (no board seam) |
| `ha_config` (+`secrets.h`) | node id, broker, NTP, WiFi creds; NVS-overridable | ADR-0020 | any | nvs | **shared → [firmware/components/](../firmware/components/ha_config/)** — c3/c6/s3-eth. Secrets seam: `secrets.h` stays board-local; app_main passes compile-time defaults in, component is secrets-free |
| `ha_wifi` / `ha_eth` | Bring up IP (onboard radio / W5500 SPI) | — | native WiFi / W5500 / esp-hosted-WiFi (panel) | — | platform |
| `ha_sntp` | Clock sync (best-effort) | ADR-0015 | any | esp_netif | **shared → [firmware/components/](../firmware/components/ha_sntp/)** — c3, c6, s3-eth (no board seam) |
| `ha_ota` | Signed, host-pinned, hash-verified A/B OTA w/ rollback + node-id identity gate | ADR-0020 | any | app_update | **shared → [firmware/components/](../firmware/components/ha_ota/)** — c3, c6, s3-eth, d1001-panel (cfg seams: log/health/radio) |
| `ha_led` | Operability LED (S3 WS2812) | — | S3 only | rmt | platform |
| `app_main` | Orchestration: nvs → config → netif(once) → net → sntp → mqtt → ble_scan → ota-confirm | — | per-device shim | — | **drifted (2×)** |
| `ha_battery` | Gauge (ADC→SoC LUT, smoothed) + IMU board-temp + thermal-gated charge mgr **+ restart watchdog** | ADR-0020 | any (board wiring is `ha_battery_cfg_t`; `_d1001_cfg()` preset) | esp_adc, io_expander, i2c (IMU) | **shared → [firmware/components/](../firmware/components/ha_battery/)** — d1001-panel |
| `ha_sdcard` | microSD mount (SDMMC+FAT); card VDD/LDO/slot are config | ADR-0020 | ESP32-P4 SDMMC (D1001 preset: slot 0, LDO ch4, VDD GPIO46); coexists with C6 SDIO on slot 1 | fatfs, sdmmc | **shared → [firmware/components/](../firmware/components/ha_sdcard/)** — d1001-panel |
| `fs_ops` | SD file-ops over MQTT (ls/stat/read/write/rm/mkdir/df); worker+queue, base64, `/sdcard`-scoped | ADR-0020 | any node with a mounted FS + MQTT sink | fatfs, json, mbedtls | **shared → [firmware/components/](../firmware/components/fs_ops/)** — d1001-panel |
| `bat_profile` | Panel glue: mount (`ha_sdcard`) + append battery telemetry CSV every 15 s + MQTT mirror | ADR-0020 | panel-local (composes `ha_sdcard`+`ha_battery`) | — | **fork (panel-local)** |
| `sgp40` | Sensirion SGP40 VOC sensor I2C driver (measure_raw + self-test/wiring-check; RH/T compensation) | ADR-0020 | any node with an I2C bus (bus/pins injected) | driver (i2c_master) | **shared → [firmware/components/](../firmware/components/sgp40/)** — c6-bench |
| `sgp30` | Sensirion SGP30 eCO₂+TVOC sensor I2C driver (0x58; measure/self-test/baseline). Computes eCO₂/TVOC on-chip → NO `sensirion_gas_index` companion needed (cf. `sgp40`@0x59) | ADR-0020 | any node with an I2C bus (bus/pins injected) | driver (i2c_master) | **shared → [firmware/components/](../firmware/components/sgp30/)** — esp32c6 (`cbed_c6`/`Cbed_C6_gsens`, via `HA_GAS_SGP30`) |
| `sensirion_gas_index` | Raw SGPxx SRAW → 0..500 VOC/NOx Index (adaptive baseline; 1 Hz) — vendored BSD-3 | ADR-0020 | any (pure math) | none (pure) | **shared (vendored) → [firmware/components/](../firmware/components/sensirion_gas_index/)** — c6-bench |
| `ha_gas` | Node glue: I2C bus + gas sensor at 1 Hz → node-local MQTT publish; dual-role alongside BLE relay; no-op-safe if unwired. **Compile-time sensor select** (`HA_GAS_SGP30`): SGP40→VOC Index (`sgp40`+`sensirion_gas_index`) *or* SGP30→eCO₂/TVOC (`sgp30`). Reg key derived from `HA_NODE_ID`. | ADR-0020 | esp32c6 (`coffice_c6` SGP40, `cbed_c6` SGP30) | — | **fork (node-local)** |
| `sqlite3` | Vendored SQLite 3.45.1 amalgamation + a minimal FATFS VFS (no-op locking; single-writer/reader only). Storage substrate for the ADR-0022 rung DB on SD. **Validated on P4**: 1001-row indexed range query = 64 ms cold / 8.9 ms warm out of a 518 k-row / 37 MB table (74 B/row, INTEGER ts + WITHOUT ROWID). Two hard gotchas in its README (IDF's arg-evaluating `assert` under NDEBUG; FatFs `lseek`-past-EOF extends on **read**). | ADR-0022 | any node with a FAT-mounted SD | fatfs, vfs, esp_hw_support | **shared (vendored) → [firmware/components/](../firmware/components/sqlite3/)** — validated, **not yet consumed** (awaits `ha_replica`, gated on `rollup-ladder-server`); not in the usage matrix until a build `REQUIRES` it |

## Extraction order (ADR-0020 Stage 1)

Extract the **shared + pure** first (lowest risk): `switchbot_decode` **✓ done** (verbatim,
[firmware/components/switchbot_decode/](../firmware/components/switchbot_decode/), host test passes; consumed
by no build yet), then `ble_scan` (with a platform hook for native-controller vs
`esp_hosted_bt_controller_init`+VHCI). Panel adopts these first; live edge nodes migrate gated. Reconcile the
**drifted** `ha_mqtt`/`app_main`/`ble_scan` into one parameterized module during migration.
