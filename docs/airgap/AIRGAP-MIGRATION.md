# Air-gap migration — requirements, final architecture, and learnings (durable)

The distilled, durable record of moving the whole home-automation fleet from the household network onto the
air-gap network. The blow-by-blow execution journal is [`MIGRATION-DESIGN-LOG.md`](MIGRATION-DESIGN-LOG.md);
**this** file is the stable "what it had to do, how it ended up, and what bit us" reference. Verify any device
state live before trusting it (see Learning #8).

## 1. Requirements & constraints (what the migration had to satisfy)

- **Move every device** off the household net (`192.168.0.0/24`, dictator `.210`) onto the **air-gap net**
  (`192.168.1.0/24`, dictator **ha-2 @ `192.168.1.210`**). Panels last; timing flexible.
- **Preserve the dehumidifier ↔ `meter_pro_living_room` control pairing** end-to-end (the one hard constraint;
  everything else was flexible). Achieved — ha-2's controller drives it, verified firing.
- **`.210` stays a PERMANENT dev platform + web bridge**, NOT the air-gap failover. Two web endpoints: prod
  `:443` (bridge → ha-2), dev `:8443`/own stack. New devices are born on `.210` then repointed to ha-2.
  ([[210-dev-platform-two-endpoints]]).
- **Redundancy in capability is a first-class goal** (Hugh): prefer overlapping/backup capability. E.g. Aranet
  radon decode on BOTH S3s + ha-2's own scanner; BLE meters dual-heard. Don't strip a capability just because
  another node has it. ([[redundancy-is-a-design-goal]]).
- **Cloud-appliance "never open/again" & local control**: control is fully LOCAL (Midea LAN key/token; Levoit
  recorded `ota_password`). The vendor app / a USB reflash is a *provisioning* step only, done once.
- **Data storage is primary**: collecting + STORING sensor data (incl. derived) is a requirement of every
  server activity; deletion only on explicit request.
- **Everything in code + documented; verify before trusting** (Hugh, standing — see §4). The board and older
  docs are SUSPECT leads, not truth.

## 2. Final architecture (verified live)

- **Air-gap dictator = ha-2** (`192.168.1.210`): runs the stack (broker, ingest, storage, BFF/`ha-api`,
  control, scanner). Broker is `allow_anonymous` on-LAN. It is **air-gapped — no internet, cannot `git pull`**;
  code reaches it by **rsync from `.210` (the dual-homed bridge)**, not from GitHub.
- **`.210` = dev platform + bridge**, dual-homed (household `enp4s0` + air-gap `wlp2s0`/`.245`).
- **Wifi:** SSID `autohome_airgap` (WPA2, PSK = the household WiFi password). **Kept UN-HIDDEN** — see Learning #1.
- **On ha-2 (verified):** C6 gas fleet, both S3s (`hbed_s3` office-closet W5500 radon relay; `s3-crawlspace`
  kitchen SGP40 + redundant radon), Midea dehumidifier (`.119`, DHCP-pinned, controlled), Levoit purifier
  (`.148`), `plug_g11`, the 2 PMs, and both panels (D1001 `.110`, E1001).
- **BLE meters + radon are DUAL-HEARD** (broadcasts): both `.210`'s and ha-2's scanners record them. Radon on
  1.0 is served by ha-2's own scanner (~60 s). Not "migrated" per se; dual-recording is accepted redundancy.
- **Panels:** D1001 repoints at runtime (native `esp_wifi` + DJ-19 signed `op:repoint`, self-heals); E1001 is
  ESPHome (rebuild+OTA). Both fetch `/api/v1/panel/tiles` from ha-2's BFF.

## 3. Learnings & whoopsies (with the fix — so they don't recur)

1. **ESPHome cannot join a HIDDEN SSID.** Verified exhaustively on the Levoit (C3): plain scan, `fast_connect`,
   AND BSSID-pin + `fast_connect` all FAILED; it joined instantly (−37 dBm) only when un-hidden. The native-C
   fleet joins hidden fine (directed probe); ESPHome does not. → **Keep `autohome_airgap` un-hidden; un-hide
   it during device intake.** ([[intake-unhide-ssid]]). Applies to E1001 too.
2. **Do NOT reboot a low-battery panel.** D1001 was online (just display-off from the dead-battery power
   policy); a reboot dropped it into the boot-confirm cycle and it brownout-stalled. There is no clean
   "display-on" command — if a panel is on wall power, **let it charge**, don't force a reboot.
3. **Verify ha-2's BFF serves the PANEL endpoints BEFORE cutting a panel over.** ha-2's server was several
   commits behind and 404'd `/api/v1/panel/tiles` → panels showed "no data." Fix = **rsync `server/` from
   `.210` → ha-2 + restart `ha-api`** (ha-2 is air-gapped; deploy via the bridge). Panels display data only
   once ha-2's BFF has their route.
4. **Cloud appliances (Midea):** the vendor app is for WiFi (re)association ONLY, not control. **Never
   factory-reset** (rotates the LAN key/token); a network change preserves them. Flag the app as a
   provisioning dependency at intake/repoint. ([[midea-app-dep-control-vs-provisioning]]).
5. **`device_push` has no `node:server`/LAN-actuator class** → Midea/Levoit skip the pending-hold retire; had
   to `set_pending` manually. Follow-up: add the class (board `device-push-actuator-class`).
6. **BLE "migration" is dual-hearing, not a move** — retires self-heal while `.210`'s scanner runs; a broadcast
   device leaves `.210`'s records only if you stop `.210`'s scanner. ([[migration-ble-retire-after-relays]]).
7. **Fleet OTA-reject (dangling node_id)** → break-glass `unknown@` OTA to unblock, then normal OTA.
   Cable-flash fresh nodes first ([[edge-ota-node-id-cable-flash-first]]).
8. **Board & docs are SUSPECT — verify live/git.** Confirm device state by DHCP lease / broker LWT / fresh
   `device_last_seen` / MQTT topic, or dev work by `git log`. NB **git author is `heisenman` for ALL instances**
   → git proves work happened, not who owns it. ([[trust-but-verify-device-function]]).
9. **Use the board's LABELED programming header, not the generic chip pinout** — the Levoit C3's header is
   `EN|GND|VCC|TXD|RXD|IO0` (IO0 = boot strap), not the generic C3 GPIO9. ([[levoit-programming-header]]).
10. **`esphome upload` does NOT compile** — run `esphome compile` first, then `upload`.
11. **`repoint_node --wait` / departure watch false-fails on LWT lag** — the authoritative signal is arrival on
    the NEW broker (device_push confirms via the bridge), not departure from the old one.
12. **The air-gap has NO authoritative time source (no internet).** Panels/devices can only NTP-sync from a
    server that actually SERVES time. ha-2's clock was correct but (a) it wasn't serving NTP (panels drifted —
    E1001 showed a wrong wall clock) and (b) it has **no chrony sources**, so it's undisciplined and will drift.
    Fix applied: ha-2 chrony `allow 192.168.1.0/24` (⚠ **NO inline `#` comment** — chrony fatals on it), panels
    keep `ntp = 192.168.1.210`. RTC (PCF85063) holdover covers gaps. **Proper fix (remaining §5):** `.210`
    (internet-connected, dual-homed) should serve NTP INTO the air-gap so ha-2 stays disciplined.

## 4. Standing directives this migration reinforced

- **Everything in code + documented** (Hugh): after the whoopsies, all changes go through committed code and
  get documented; no bench-only / undocumented state. This file + the design log + the memories are that record.
- **Trust but verify** device function before relying on it in planning/execution ([[trust-but-verify-device-function]]).
- **Let the user steer** — don't run ahead of Hugh's direction ([[let-user-steer-dont-run-ahead]]).

## 5. Remaining after the migration (not device moves)

- `device-push-actuator-class` (Learning #5). Decide whether to ever stop `.210`'s redundant scanner (Learning
  #6 — redundancy says keep). Split-brain tidy: `.210`'s `ha-controller` still points at the dead `.0.211`
  dehum on purpose — do NOT repoint it. **ha-2's codebase is behind `.210`'s** (only `server/` was synced for
  the panel fix) — a fuller ha-2 sync is worth scheduling.
- **Establish the air-gap TIME authority** (Learning #12): `.210` (internet-connected) should serve NTP INTO
  the air-gap so ha-2 stays disciplined; today ha-2 serves the panels but has no upstream, so the whole
  air-gap drifts together. Also fold ha-2's `chrony allow` into committed provisioning config (it's a live
  edit right now).
