# MODULES.md — edge/panel firmware module catalog

The tree of firmware modules and what each provides. Pairs with [MATRIX.md](MATRIX.md) (which build links
which). Target = real shared IDF components (ADR-0020); today most are shared by `cp -r` fork.

> Status legend — **shared:** byte-identical across forks (safe to extract first). **drifted:** diverged per
> target (needs reconciliation on extract). **platform:** per-device by nature.

| Module | Role | Contract/ADR | Platform support | Dep notes | State |
|--------|------|--------------|------------------|-----------|-------|
| `switchbot_decode` | Advert bytes → reading (pure, has test) | device-family decode | any | none (pure) | **shared** |
| `ble_scan` | NimBLE passive observer → decode → publish; transport-aware duty cycle | ADR-0001 | native-radio **or** esp-hosted-VHCI (panel) | NimBLE | drifted (c3/c6 vs s3) |
| `gatt_exec` / `gatt_history` | Server-driven GATT actuation / history pull | ADR-0010 | native-radio **or** VHCI | NimBLE central | shared |
| `ha_mqtt` | Broker client: adverts/status/log; subscribes signed cmd + relay; verifies HMAC | ADR-0010 | any transport | mqtt | **drifted (3×)** |
| `ha_relay` | Phase-B coverage filter (signed `relay_assign`, NVS, epoch-guarded) | ADR-0015 | any | nvs | shared |
| `ha_config` (+`secrets.h`) | node id, broker, NTP, WiFi creds, command secret; NVS-overridable | — | any | nvs | platform |
| `ha_wifi` / `ha_eth` | Bring up IP (onboard radio / W5500 SPI) | — | native WiFi / W5500 / esp-hosted-WiFi (panel) | — | platform |
| `ha_sntp` | Clock sync (best-effort) | — | any | — | shared |
| `ha_ota` | Signed, host-pinned, hash-verified A/B OTA w/ rollback | — | any | app_update | shared |
| `ha_led` | Operability LED (S3 WS2812) | — | S3 only | rmt | platform |
| `app_main` | Orchestration: nvs → config → netif(once) → net → sntp → mqtt → ble_scan → ota-confirm | — | per-device shim | — | **drifted (2×)** |
| `ha_battery` *(planned)* | Fuel-gauge read → `batt_pct`/`charging` in status heartbeat | — | panel-class (has battery) | i2c | **new** |

## Extraction order (ADR-0020 Stage 1)

Extract the **shared + pure** first (lowest risk): `switchbot_decode`, then `ble_scan` (with a platform hook
for native-controller vs `esp_hosted_bt_controller_init`+VHCI). Panel adopts these first; live edge nodes
migrate gated. Reconcile the **drifted** `ha_mqtt`/`app_main`/`ble_scan` into one parameterized module during
migration.
