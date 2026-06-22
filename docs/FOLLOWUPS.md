# Follow-ups & clarifications for Hugh

## 🟡 CONTROL PLANE GO-LIVE — code DONE, awaiting .245 deploy (2026-06-21)
Authenticated control API wired + tested (68 tests green); built in the **server folder**
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
- Outdoor history read (`02` reject on attic/h_bed) — needs an app HCI-btsnoop of an attic/h_bed pull. LOW (ADR-0009).
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
