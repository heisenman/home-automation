# E1001 handoff — 5c broker repoint + open ops items (dev → ops)

> **From:** `dev` (failover-finish / 5c sprint, 2026-07-10). **To:** ops (E1001 is ops-domain,
> [[dev-ops-ownership-line]]). **Why handed off:** E1001's repoint is an ESPHome **reflash** (different
> toolchain from the signed-MQTT repoints dev is running on the native-C / Tasmota fleet). Bundling the
> known E1001 items so whoever picks it up has the full context.

## 1. 5c broker repoint (the piece from dev's 5c work)
Repoint E1001 from ha-2's **host IP `192.168.1.210`** → the floating **VIP `192.168.1.200`**, so a keepalived
failover actually keeps the panel connected. This is the E1001 slice of the fleet-wide 5c repoint dev is doing.

- **File:** `provisioning/reterminal/e1001/e1001.yaml`
  - `broker:` (≈ line 226) `192.168.1.210` → **`192.168.1.200`**  ← the change
  - **Leave `bff_base_url` (≈16) and `ntp_server` (≈18) at `192.168.1.210`** — dev is doing **broker-only float**
    across the whole fleet (the standby doesn't serve BFF/NTP on the VIP yet; floating those is a separate
    later item). Match that so the fleet is consistent.
- **Mechanism:** ESPHome has no runtime NVS overlay → **edit + OTA/reflash** `e1001-c-office`.
- **⚠️ Build in an ISOLATED esphome env, NOT the shared `venv/`.** esphome pins `paho-mqtt<2` and will silently
  break every `ha-*` service on its next restart. See `docs/coord/SHARED-VENV-ISOLATION.md`.
- **⚠️ PRESERVE `api: reboot_timeout: 0s`** (dev added it 2026-07-10, commit 6e29d94 — it stops the ~15-min
  no-client reboot loop; [[esphome-api-reboot-timeout]]). Don't let a reflash regress it.

## 2. Verify after reflash
- On ha-2: `ss -tnH state established '( sport = :1883 )' | grep 192.168.1.200:1883` → E1001's IP should appear
  on the **VIP listener** (this is how dev is verifying every 5c repoint — landing on `.200` = success).
- Uptime climbs past ~15 min (confirms the reboot_timeout fix survived).
- Panel still publishes `e1001-c-office/#` (sensors, battprofile).

## 3. Quarantine catch — E1001-C-OFFICE-SHT40 (needs HUGH's decision, not delegable)
The new quarantine-DB (ADR-0032) caught `edge/E1001-C-OFFICE-SHT40` (panel onboard SHT40 temp/RH) live-but-
unregistered on ha-2 → silently dropped since the panel repoint; now accumulating in `instance/db/quarantine.db`.
**Decision = register vs purge** (redundant with `meter_pro_c_office`?). Tee it up for Hugh; `tools/quarantine.py
list/inspect` shows what's accrued. See [[migrated-device-silent-drop]].

## 4. Existing ops board items (fold in)
- `e1001-finish-features` — see `docs/design/e1001-gap-register.md`.
- `e1001-sd-backup-explore`.
- `ota-verify-window-rollback` (relevant to doing the reflash safely).

## Not in scope for E1001 here
Dev owns and is handling the rest of the 5c fleet (native-C edge via `repoint_node.py`, D1001 via
`d1001_cmd.py`, Tasmota via `repoint_tasmota.py`, Levoit ESPHome). Only **E1001** is handed to ops.
