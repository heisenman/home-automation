# Follow-ups & clarifications for Hugh

## ✅ CHECKPOINT 2026-06-25 — AUTHORITATIVE current state (supersedes everything below)
Verified against the live cluster + source this date. Repo `801545f`.

**SHIPPED + VERIFIED LIVE today (210 dictator / .245 warm standby):**
- **R9 API security (ADR-0017) — mechanism LIVE.** HTTPS:8443 (`ha-api-tls`, self-signed SAN incl VIP),
  `/auth/login`+`/auth/refresh`+dual-verify (JWT-or-legacy, admin-gated), PWA `crypto.subtle`. `:8123`
  untouched (healthcheck-safe). cert+VAPID synced in `dictator-files.manifest`. **DEFERRED to post-air-gap
  (Hugh):** operator/viewer per-route split, legacy-bearer deprecation, JWT-login PWA adoption, local-CA.
  Reads stay OPEN on the LAN.
- **Notifications: vendor Web Push DROPPED → MQTT + self-hosted ntfy (E2E verified).** SW won't register on
  a self-signed cert (needs local-CA) + Web Push needs FCM/Mozilla cloud = air-gap-fatal. Now: alert engine
  publishes `home/_alerts` (retained) + `home/_alert/new` (edge) on the dictator; `ha-ntfy-bridge` → self-
  hosted `ntfy:8095` → phone (smoke test reached ntfy HTTP 200). In-app banner kept (confirmed firing on
  the master-bath disconnect). See [air-gap-notify.md](decisions/air-gap-notify.md).
- **Failover HISTORY RECONCILIATION (ADR-0016) — LIVE + verified.** `reconcile-history.sh` bidirectional
  windowed `hot.db` merge over id_cluster SSH; `ha-reconcile-history` VIP-gated 15-min proactive loop +
  `notify.sh` MASTER/BACKUP hook; cluster-doctor convergence check. Live proof: bidirectional merge +
  **convergence Δ0** (primary=standby=14691). Shadow tuner logs a proposed adaptive interval (15 min stays
  fixed). **Closes the data-plane gap — the gate before trusting operational failover.**
- **House scenes (Home/Away/Sleep)** LIVE (ADR-0011 addendum); **phase-b-notify-failover** DONE (notify.sh
  binds ha-relay-coordinator to VRRP role); **openwrt-prestage** DONE (R7800 offline image+config+runbook,
  single-bridge design fixes vip-from-wifi); **add-device-flow** DONE (dev: sensor/actuator/node-enroll).

**ACTUALLY OPEN / NEXT:**
1. **Reconcile shadow-tuning review — SCHEDULED 2026-07-02** (cloud routine `trig_01WsViJjLPtMsu3i93qqKCk4`,
   reminder only — runs LAN-side). Read `instance/ha-reconcile-tuning.log`; sane → flip `RECONCILE_MODE=active`
   on 210+.245; weird → revisit tuner (ADR-0016).
2. **Failover DRILL** (dev harness, `failover-drill`) — now meaningful for the data plane; seize→release→
   confirm zero history hole = the live end-to-end proof. Gated on Hugh OK + window.
3. **OpenWRT cutover** (theme B, router ETA ~2026-07-09, Hugh + window) — prestage READY; clears
   `vip-unreachable-from-wifi` + `broker-auth-posture` + `network-init-tooling`.
4. **Deferred (post-air-gap):** the R9 role/legacy/PWA-JWT items above; **notify-HA** (ntfy+bridge on the
   standby); web-push remnant cleanup; **parquet deep-reconcile** (ADR-0016 deferred lift, device-buffer
   deadline-bounded).

---

## ✅ RECONCILED 2026-06-23 — AUTHORITATIVE current state (everything below is historical log)
Reconciliation after the firmware items below were found stale (node had moved past them). Verified
against the live system + source on this date.

**DONE since written (the older sections below are kept as a record, not a to-do list):**
- **Edge firmware = `v9-bankts`**, running on c6-bench. It already INCLUDES everything the
  "NODE-SIDE LOCKDOWN" + "OTA image-hash verify" items below asked for — verified in source:
  GATT-writes-off-by-default (`gatt_exec.c` `HA_ALLOW_GATT_WRITE=0`), OTA host-pin (`ha_ota.c`
  `HA_OTA_HOST="192.168.0.245"`), and OTA image-hash verify (`ha_ota.c` mbedtls SHA-256 + anti-downgrade).
  → the 🟡 lockdown section and the "OTA image-hash verify" queued item are **OBSOLETE**.
- **`.112` duplicate stack retired** — API (8123) + mosquitto (1883) down/refused (verified from .245).
- **c_office meter battery** = 74% (not 1–2%) — swap item moot.
- **Automation controller + full PWA** — LIVE (control loop, override/policy/manual API, view-model BFF,
  multi-source graphs, °F/°C). ADR-0011 status corrected to Accepted/live.

- **Outdoor-meter history backfill — WORKING (verified 2026-06-23).** The `02` outdoor-read reject is
  resolved in practice: h_bed / c_office / h_bath / attic all have recent **`ble-history`** rows in
  parquet (e.g. h_bed 2,795 rows 6/21→6/22, attic 11,580 6/14→6/22). Pulls run **server-side** (Bleak,
  `server:server>meter_…`); the only recent pull_log failures are transient `connect_fail`/`empty_buffer`
  (range / nothing-new), NOT the format reject. → the btsnoop root-cause + CSV-gap items are MOOT.

**ACTUALLY OPEN (updated 2026-06-24):**
1. **G11 provisioning bring-up** (hardware) — Stage 2 DONE; **`ha-dev` (210) is the LIVE DICTATOR** as of
   2026-06-24. Unblocks Secure-Boot/flash-enc (Phase 8); OTA-host pin still to move off .245 (with c6-bench).
   - ✅ **DONE:** §4 packages (bluez 5.82, mosquitto 2.0.21), §5 BlueZ `--experimental`, §6 venv on
     Py3.13 (pyarrow→18.1.0, install.sh `python3-venv`), §7 units installed; `ha-writer/api/edge-mapper/
     edge-history` + compactor/verify-hashes/gap-watcher timers **active**; `ha-scanner` **active** on the
     onboard MT7922 (`hci0`). **Registry loaded + data LIVE (eve 2026-06-24):** the real
     `instance/devices.yaml` was present but services had started before it — a `ha-writer`+`ha-scanner`
     restart loaded it; **10 of 11 meters now decode to `home/<area>/<device>/state`, `/devices` +
     `/api/v1/sensors` populated, dashboard shows data.** `gh` authed (heisenman); repo pushed through
     `ceeba71`. New: `provisioning/stage2-finish.sh` — idempotent no-LLM Stage-2 finisher (+ `04-post-install.md`).
   - ✅ **BT TRIAL PASSED (2026-06-24):** onboard MT7922 ran ~9.5h overnight with `NRestarts=0`, 0
     "discovery appears dead" stalls, all 10 meters fresh through the morning. **Decision: keep the onboard
     radio — UB500 dongle NOT needed.** ESP32-C6 Wi-Fi relay remains the documented booster for the
     attic/crawlspace corner — `edge/esp32c6/dev-box-relay.md` (deferred: c6-bench feeds .245; use a spare or
     repoint only at cutover; doc now points the C6 at the new `.210` broker).
   - ✅ **STATIC IP CUTOVER DONE (2026-06-24):** box moved `192.168.0.150` → **`192.168.0.210`** (edited
     `/etc/network/interfaces`, backup at `/etc/network/interfaces.bak-150`, applied via reboot). Reconnected
     at .210; all services + 10 meters returned. **Done BEFORE any .245 decommission, per plan.**
   - ✅ **REBOOT TEST (§9) PASSED (2026-06-24):** folded into the IP cutover — after reboot all
     `ha-*`/infra services auto-returned `active`, `ha-scanner` resumed on `hci0` (`NRestarts=0`), data
     flowing within ~40s. `ha-controller` correctly stayed inactive.
   - ✅ **PROMOTED TO DICTATOR (2026-06-24, gated cutover with desktop Claude via `docs/cutover/`):** 210's
     `ha-controller` is LIVE (sole dictator; first decision held the Midea OFF → continuity preserved),
     `ha-api` control plane **MOUNTED** (no-bearer command = 401), weather lane enabled. `.245`'s controller
     was stopped + disabled + unlinked → **no split-brain**. `.245` is now a **warm-standby
     failover — LIVE + TESTED 2026-06-24** (keepalived/VRRP, primary-supremacy auto-demote; full
     fail-over/fail-back cycle passed incl. real Midea actuation — see `failover/` + `docs/cutover/`).
   - ✅ **Aranet RESOLVED — LOCAL to 210, no relay needed:** the `0x0702` or_pattern fix (`ec8511d`) works for
     passive ext-adv after all (the Aranet just advertises slowly; the first 95 s sample missed it). 210 hears
     it directly (`transport: ble-adv`, RSSI −71/−89, MAC `F4:37:5A:68:9F:1A`). **No `.245` bridge —
     `edge/aranet-245-relay.md` is SUPERSEDED.** Robustness: −89 is edge-of-range; durable fix = the
     **ESP32-S3-ETH wired edge node** (deferred post-handoff).
   - ✅ **Weather lane DONE:** `instance/weather.env` present + populated; `ha-weather.timer` enabled, recording
     open-meteo readings to `weather.db`.
   - 🔲 **STILL OPEN (210 as dictator):** (a) **sudo hardening** — password set; narrow sudoers to `ha-services`
     + drop the broad bootstrap grant (`stage2-finish.sh --narrow-sudoers`), do **LAST** (remaining setup needs
     sudo); (b) **broker auth/ACL posture** — 210 is anonymous-on-LAN, `.245` had auth; decide replicate (we
     have `mqtt.env`) vs stay anonymous — couples with (c); (c) **c6-bench repoint** off `.245`→210 (+ OTA host
     pin .245→.210) — edge work, post-handoff; (d) ✅ **failover signaling DONE** (keepalived/VRRP
     VIP + primary-supremacy auto-demote + heartbeat RPC + state-sync; full cycle tested live 2026-06-24,
     `failover/`; remaining refinement = cross-node MQTT `ha/cluster/#` bridge + notify startup-transient);
     (e) **ESP32-C3** edge-node dev.
     §3 dual-NVMe + §8 data-migration N/A here.
2. **PWA/automation software** (ADR-0014):
   - ✅ **R8 device friendly-name/room/hide — BUILT 2026-06-23 (`e81ff34`).** (Still TODO under R8:
     add-new-device + explicit retire-vs-hide.)
   - ✅ **Alerts — BUILT 2026-06-23 (`9208852`)**: low-battery/unreachable/tank-full/override-expiring
     via `GET /api/v1/alerts` (server-side rules, reusable by MCU/push) + a PWA banner. *(Web Push
     delivery deferred — needs push subscription + SW push handler.)*
   - **R9** auth roles + token expiry/rotation + TLS — deferred to ride with the G11 (TLS wants a cert;
     reads are already open on the LAN so a viewer role is moot until reads are gated).
   - ✅ **decision-history "why is it on?"** — BUILT (`4d3da77`, v16): card expander of the last 8 control_log entries.
   - ✅ **strategy-in-UI** (hysteresis vs setpoint) — BUILT (`4d3da77`, v16).
   - ✅ **source-fallback chain** — BUILT (`d7ac15f`, v18): ordered `fallback_sensors`; controller uses
     the first fresh one, logs "(via fallback X)".
   - ✅ **calibration offsets (display-only)** — BUILT (`d7ac15f`, v18): per-(device,metric) offset added
     to chips + graphs; control reads raw. (Decision 2026-06-23: display-only.)
   - ⏸️ **richer schedules + modes/scenes (Away/Home/Sleep)** — DEFERRED (decision 2026-06-23): low value
     with a single actuator; revisit when there are multiple devices to coordinate.
   - ✅ **dew point** — BUILT (`0631caa`, v20): for any sensor with temp+RH, shown in chip + expanded
     (labeled "Dew", v21) and graphable (`?metric=dewpoint_c` computes a paired series). RH kept.
   - ✅ **admin-login bug FIXED** (`166e935`, v19): `crypto.subtle` needs HTTPS/localhost → undefined on
     plain-HTTP LAN, hung the login. Now a pure-JS SHA-256 (self-tested) + non-hanging submit. (Off-LAN
     TLS still the proper fix → with G11.) **PWA is at build v21; all features above are live.**
3. ✅ **Small code cleanups — DONE 2026-06-23 (`4b14d73`)**: removed dead `_parse_env_file`; wrapped the
   `/devices/{id}/command` handler in `run_in_threadpool` so a Midea LAN command (≤40s subprocess) no
   longer stalls the async API. *(Midea token ~18h rotation → folded into the alerts work under #2.)*
4. **(deferred)** per-node nonce/counter to close the 60s ts-replay window; Secure-Boot v2 + flash-enc +
   anti-rollback eFuse → G11 / Phase 8.
- *Operational note:* server-side history pulls see occasional transient BLE `connect_fail` (range) — a
  retry/scheduling concern, not a bug. Not tracked as an action item.

---

## 📋 ~~TOMORROW (2026-06-22)~~ — DONE/MOOT (outdoor history now backfills server-side; see reconciled section)
## 📋 TOMORROW (2026-06-22) — two app-side tasks while Hugh is in the SwitchBot app
1. **Backfill attic/h_bed gap from app CSV** — tonight's broker cutover/flash left ~6–12 min ingestion
   gaps (all sensors briefly; live feed fully recovered). attic/h_bed minutes are BLE-unrecoverable (the
   `02` outdoor-read reject), so re-import an app CSV. ⚠️ **Use `--tz America/Los_Angeles`** (the app
   exports phone-local; wrong TZ reintroduced the original duplicate-band). `import_switchbot_csv.py` is
   idempotent (INSERT OR IGNORE) so overlap is safe.
2. **Capture an app HCI-btsnoop of an attic OR h_bed history pull** (NOT living_room) → the missing piece
   to root-cause the `02` reject (ADR-0009). Android: Developer Options → enable "Bluetooth HCI snoop log"
   → open the attic meter's history in the SwitchBot app → pull the btsnoop log
   (`/sdcard/.../btsnoop_hci.log` or via `adb bugreport`). Then diff the app's read sequence for THIS
   variant vs ours. Why it matters: our format works for living_room_outdoor but attic/h_bed reject it —
   they return ONE 0x69 metadata packet vs living_room's TWO, i.e. a firmware/revision variant the app
   adapts to and we don't. Implement the corrected sequence in `gatt_history.c` (dedicated path still
   writes; no need to re-enable the v4 forwarder write-lockdown).

## ✅ NODE-SIDE RAW-GATT/OTA LOCKDOWN — DONE, shipped in `v9-bankts` (reconciled 2026-06-23; section below is the original 2026-06-21 plan)
Two cheap least-privilege guards (defense-in-depth on top of the existing HMAC signature). Code in
`edge/esp32c6/main/`, `HA_FW_VERSION` → `v4-lockdown`. **Not yet built/flashed** (ESP-IDF + C6 live on
.112 — build there after `git pull`):
- **GATT writes OFF by default** (`gatt_exec.c`, `HA_ALLOW_GATT_WRITE=0`): `write`/`writeseq` steps refuse
  ("telemetry-only node"); `sub`/`read`/`collect` unaffected. Removes the arbitrary-BLE-write authority a
  validly-signed cmd could otherwise wield. An actuator firmware sets `HA_ALLOW_GATT_WRITE=1`.
- **OTA host pin** (`ha_ota.c`, `HA_OTA_HOST="192.168.0.245"`): rejects OTA URLs whose host ≠ the dictator.
  Image-hash gate still applies on top. **Decision (Hugh): pin to .245 now; the G11 supersedes it once
  configured** (then change the define + serve from G11).
- **⚠️ Post-v4 OTA workflow change:** once v4 runs, OTAs must be SERVED FROM .245. Build on .112 → `scp`
  the `.bin` to .245 → run `edge_ota.py --serve-ip 192.168.0.245` there. (The v4 flash itself works from
  .112 — the current v3 node has no pin yet.) Dev alt: build with `-DHA_OTA_HOST=\"192.168.0.112\"`.
- Still-open cheap item (deferred): per-node nonce/counter to close the 60 s ts replay window on authority
  ops. Heavy items (Secure Boot v2 + flash-enc + anti-rollback eFuse) → G11 provisioning phase.

## ✅ CONTROL PLANE GO-LIVE — DEPLOYED & VERIFIED on .245 (2026-06-21)
Live: `dashboard=200`, `no-bearer=401`, `bearer+unknown-device=404` (cryptography installed; control
router mounted). Authenticated control API wired + tested (68 tests green); built in the **server folder**
(`/home/visko/Desktop/Profile/home_automation` = the CIFS-mounted `.245:~/home_automation`, per your
"new work → server folder" instruction). What's live in code:
- `/devices/{id}/command` mounts into the live API **only when the master passphrase is present** (else
  read API runs unchanged — never unauthenticated control). Graceful-degrades on any config error.
- **Admin auth** = `Authorization: Bearer SHA256("ha-api:"+master)` (your SHA-derive choice). **Confirm
  token** `SHA256("ha-confirm:"+master)` stays the separate 2nd factor for sensitive actions.
- PEP sources each device's HMAC key from the encrypted **LUT** by node (`secrets_from_lut`).
- `ha-api.service` gains `HA_MASTER_PASS_FILE` + `EnvironmentFile=-instance/mqtt.env`.
- Fixed a latent FastAPI bug (typed Request/Response params + `from __future__ annotations` → 422) and
  hardened `MqttTransport` (broker-down → clean 504, not a 500).
**TO GO LIVE (supervised, `provisioning/control-go-live.md`):** place `instance/{.master_pass,
node_secrets.enc,control.yaml}` on `.245` → `sudo ./install.sh` + `sudo systemctl restart ha-api` →
verify `control plane MOUNTED` + 401-without-bearer. **Node-side caveat:** no actuator firmware consumes
`home/<area>/<dev>/cmd` yet (c6-bench only does edge gatt/history/ota), so `control.yaml` stays minimal
until the first actuator — the server plane is the deliverable here.

## ✅ BROKER AUTH/ACL CUTOVER — COMPLETE (2026-06-21)
Broker flipped from anonymous to authenticated + topic-ACL'd on `.245`, live and verified. Identities:
**dictator** (all server services — writer/scanner/edge-mapper all authenticated + connected) and
**c6-bench** (the edge node — reconnected, `online ota_1 v3-otahash`). Anonymous pubs now refused;
ACL enforced. Code prerequisite (`server/util/mqtt_creds.py` wired into every client + `EnvironmentFile`
on the units) shipped in `db2db25`. Two gotchas hit + documented in `provisioning/broker-auth-cutover.md`:
(1) `mosquitto_passwd -c` writes the passwd file `root:root 0600` → broker (runs as `mosquitto`) can't
read it → rejects everyone even with correct creds; fix `chown mosquitto:root` + `chmod 0640`. (2) the
`instance/mqtt.env` `CHANGE_ME` placeholder connects fine on the anon broker (false positive) then fails
the instant auth flips — set the real pass before flipping. **Next gate for control go-live: API auth +
mount the control router (issuer sources node secret from the LUT; confirm = SHA(master)).**

## ✅ Attic "duplicate data" — FULLY RESOLVED (2026-06-21)
API hot-wins dedup deployed (you ran it) + deeper remediation run (`fix_meter_reimport.py`): backed up,
purged **438,068** tangled attic `csv-import` rows from parquet (~2.4× overlap), re-imported the clean
**179,396**. Verified: 6/18 band gone (single 16.78 °C), Apr 21 clean, 1440 rows/1440 unique-ts per day
(no dupes). Backup at `instance/db/backup-20260621-202947`. Next compaction folds the corrected hot
rows into parquet cleanly.

## Attic "duplicate data / one set wrong" — ROOT-CAUSE (kept for reference)
- **Cause:** a timezone-corrected CSV **re-import** landed in `hot.db` for dates already compacted to
  **parquet**, so the readings API returned BOTH rows at each timestamp → the duplicate band.
- **Which was wrong:** parquet (the pre-fix import) held **time-shifted** values; hot (the corrected
  re-import) matches the **live ble-adv sensor to ~0.2 °C** (verified 6/21). So hot is correct.
- **Fix (`005d031`):** the API now dedups hot/parquet by `(ts,metric)` — **hot wins**. Stops the band
  wherever the corrected re-import overlaps (the last ~7d).
- **Deeper remediation — TOOL READY (`tools/fix_meter_reimport.py`, validated on synthetic data).**
  Backs up first, purges the device's csv-import rows from BOTH hot.db + parquet, then re-imports a
  fresh CSV correctly. Your fresh full dump is on `.245` at `~/home_automation/attic s wall_data.csv`
  (Apr20→now, 89,700 rows, format matches the importer). **Run on `.245`** (autonomous prod writes are
  classifier-blocked, so this is yours to run — or say "run it" and approve):
  ```
  git -C ~/home_automation pull            # get the tool
  cd ~/home_automation
  venv/bin/python tools/fix_meter_reimport.py --device-id meter_attic_south_wall \
      --csv "$HOME/home_automation/attic s wall_data.csv" --area attic \
      --device-type switchbot_meter_outdoor --dry-run      # preview, then drop --dry-run for real
  ```
  No API restart needed (it reads the DB live; the hot-wins dedup is already deployed). Next 02:00 UTC
  compaction folds the corrected hot rows into parquet cleanly.

## Decision — confirm-PIN = SHA(master) ✅ (your idea, 2026-06-21)
Accepted and actually elegant: set `confirm = SHA256("ha-confirm:" + master_passphrase)`. SHA is
one-way, so the **hot** confirm value (typed/transmitted often) never reveals the **cold** master
(which encrypts the secrets LUT) — that resolves my blast-radius concern while keeping ONE secret to
manage. Master passphrase = `CHANGE_ME_master_passphrase` (stored gitignored / via env, never committed). TODO: wire the
SHA-derived confirm into the verifier + bake per-device secrets via the enrollment tool.



Running list maintained during autonomous work sessions. Newest section on top. Guiding philosophies
(stated 2026-06-21): **security over the air** + **flexible modular infrastructure** between
dictator / failover / edge nodes / endpoints.

## Decisions — ANSWERED 2026-06-21
1. **Unsupervised flashing** — ✅ Cleared to OTA without you, IF all edge-node (server-side) work is done
   first, then firmware batched. (For OTA-security I still use a safe 2-step: prove signed-OTA verify
   additively, then remove the unsigned fallback — no lockout even unsupervised.)
2. **Actuator config layout** — ✅ separate files (`instance/control.yaml` + `instance/control_secrets.yaml`). Done.
3. **Second factor = SOFTWARE only** — ✅ not all endpoints have buttons → confirm is a software PIN/token
   at the API (built: `confirm_pin` + verifier). The only *physical* path is firmware flashing, which
   should EVENTUALLY be cable-from-the-G11 (the "scary" op). OTA = dev/break-glass. (Captured in ADR-0011/0005.)
4. **Mode mechanism** — ✅ built (deadband + dwell). Real power/UPS input drivers still TBD hardware.

## ✅ Enrollment + confirm — BUILT (2026-06-21)
5. Per-device secrets: `tools/enroll_node.py` → encrypted LUT (`instance/node_secrets.enc`, master
   `CHANGE_ME_master_passphrase` in `instance/.master_pass`). Physical-presence: emit `secrets.h`, cable-flash from G11.
   c6-bench seeded. 6. Confirm = `SHA256("ha-confirm:"+master)` (`secret_store.make_confirm_verifier`).
   DEFERRED to control-go-live: mount control router + issuer sources node secret from LUT; LUT→.245.

## Done live this session (2026-06-21)
- ✅ **Signed-only commands incl OTA** — deployed C6 (now `ota_1 v2-sec`) requires a valid HMAC
  signature for every directive; unsigned OTA rejected. OTA directive authenticated.
- ✅ **Latent broker creds** compiled into the C6 (`HA_MQTT_USER/PASS`, ignored on anon broker) —
  the cutover prerequisite is now in firmware.

## Queued (need you / coordination)
- **OTA image-hash verify** (the remaining OTA-security gap): firmware should verify the downloaded
  image's SHA-256 against the signed value before flashing (defends a compromised image-server/URL).
  Needs the chunked `esp_https_ota` path (hash-before-commit). `edge_ota.py` already sends the signed
  sha256; only the firmware check is missing. Best done as the next supervised flash. (Endgame per
  ADR-0011: firmware via cable-from-G11; OTA = break-glass.)
- **Broker auth/ACL cutover** — ✅ **DONE & verified 2026-06-21** (see resolved section at top). Broker
  is authenticated + ACL'd; dictator + c6-bench live. New nodes: enroll → set their broker pass with
  `mosquitto_passwd -b /etc/mosquitto/passwd <node> '<secrets.h pass>'` + add an ACL stanza.
- **Control API** goes live only AFTER broker auth + API auth (else unauthenticated control).
- **Confirm-PIN store + admin API auth** (decision #6 above).

## Open / deferred (lower priority)
- ✅ Outdoor history read (`02` reject on attic/h_bed) — RESOLVED 2026-06-23: all outdoor meters now
  backfill `ble-history` via server-side Bleak pulls (no btsnoop needed). See reconciled section at top.
- c_office meter **battery swap** (1–2%) — physical.
- **✅ ARANET FULLY INTEGRATED (live via ha-scanner on .245 + web app + 90d history).**
- **Aranet — DECODER + LIVE RELAY DONE & validated (2026-06-21).** Corrected to mfr 0x0702 ext-adv;
  `tools/aranet_relay.py` decodes + publishes canonical state (radon/temp/pressure/humidity/battery).
  Live-validated from `.112` (radon 10 Bq/m³). Registry MAC fixed locally (placeholder→F4:37:5A…).
  **SCANNER LOCATION — RESOLVED (2026-06-21):** with the Aranet moved downstairs, **`.245` (server)
  sees it at RSSI −64** (better than `.112` at −78) and its BlueZ already receives the `0x0702` ext-adv.
  So NO dedicated crawlspace node is needed for this spot — the server can scan it. CLEANEST PERMANENT
  PATH: **teach the running `ha-scanner` to also decode manufacturer `0x0702`** (it already receives the
  packets via BlueZ) → one scanner, both SwitchBot + Aranet, no extra process. Small `scanner.py` change
  + a deploy (restart ha-scanner — your OK). History (`get_all_records`) can also run from `.245`. (90-day
  radon history already imported.)
  - **History pull DONE** (`tools/aranet_history.py`): 30 days / 4320 records backfilled from the desk
    via the `aranet4` lib (GATT), idempotent. `aranet_radon` now on the dashboard. Re-run anytime in
    range to top up. ONGOING live data needs the relay running somewhere in range (placement decision).
  - **Deploy note:** `.245`'s registry also needs the real Aranet MAC if a scanner runs there.
- Retire `.112` duplicate services (`sudo systemctl disable --now ha-api ha-writer mosquitto`) — your sudo.
- G11 provisioning bring-up (arrives ~2026-06-23) — your hardware step + on-device LLM.
