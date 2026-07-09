# SECRETS — discovery index (where each secret CLASS lives)

Canonical stores only — **actual values live in gitignored files**, never here. This index tells you where
to look so nobody has to hunt (and so a device is never bricked by a lost credential). Verify a store still
exists before relying on it. Keep this updated when a new secret class appears.

| Class | Canonical store (gitignored) | Contents | Notes |
|---|---|---|---|
| **Edge nodes** (ESP32-C6/S3 native-C) | `instance/node_secrets.enc` (encrypted; master = `instance/.master_pass`) | per-node `cmd_secret` (HMAC) + `mqtt_pass` | Managed by `tools/enroll_node.py`. `secrets.h` is re-emitted per build (gitignored, `edge/<board>/main/secrets.h`) — never the source of truth. |
| **ESPHome devices** | per-project `secrets.yaml` next to the config (gitignored; `.example.yaml` is the committed template) | `wifi_*`, `ota_password`, `fallback_ap_password` | **Levoit purifier:** `provisioning/levoit/secrets.yaml` — its `ota_password` is the **never-open-again key** (lost once → forced a teardown 2026-07-09; now recorded). **E1001 panel:** `provisioning/reterminal/e1001/secrets.yaml`. |
| **Broker creds** | `instance/mqtt.env` | `HA_MQTT_USER` (`dictator`) + `HA_MQTT_PASS` | Household + ha-2 brokers are `allow_anonymous true` on-LAN today; edge nodes still use per-node creds. |
| **Air-gap router** | `instance/openwrt/airgap_router.env` | `WIFI_PSK` (`autohome_airgap`), regulatory, static-lease MACs (`HA2_MAC`, `BRIDGE_MAC`, `MIDEA_MAC`) | Substituted into `provisioning/openwrt/etc/config/*` by `router_reconcile.py`. |
| **Midea dehumidifier** | `instance/midea-device.env` | `MIDEA_DEVICE_ID` / `KEY` / `TOKEN` / `IP` | LAN control creds (survive a WiFi change, NOT a factory reset). Synced to ha-2. |
| **Master passphrase** | `instance/.master_pass` | unlocks `node_secrets.enc` | Also `$HA_MASTER_PASSPHRASE`. Guard this — it gates the whole edge fleet. |

**Rule (per project directive):** store each secret canonically in its class store above; when a new device/
class is onboarded, add its store here. Env files must be `source`d, not awk/tr-scrubbed (mangles quoted
values). See memory `secrets-canonical-and-documented`.
