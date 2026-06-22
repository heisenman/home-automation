# Follow-ups & clarifications for Hugh

## 🔴 ACTION NEEDED FROM YOU (2026-06-21, late)
1. **Deploy the attic duplicate-data fix** — committed + pushed (`005d031`) but the live deploy to
   `.245` was blocked by the safety classifier (needs your explicit OK). Run on/against `.245`:
   ```
   git -C ~/home_automation pull && sudo systemctl restart ha-api
   ```
   (or tell me "deploy it" and I'll do it). After restart, the attic 7d graph band disappears.

## Attic "duplicate data / one set wrong" — ROOT-CAUSED + fixed (deploy pending)
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

## Still open for you
5. **Per-device secret distribution / enrollment** — model is physical-presence/console (plan §13). Confirm
   before scaling past one node (vs. a provisioning USB from the G11).
6. **Confirm-PIN storage** — where the software confirm PIN(s) live (per-device? one admin PIN?) and how the
   admin UI collects it. Currently the verifier is a pluggable callable; needs a real store + API auth.

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
- **Broker auth/ACL cutover** (`provisioning/broker-auth-cutover.md`) — now just: create passwd
  (dictator + c6-bench=the latent pass in secrets.h), give services dictator creds, flip. Supervised.
- **Control API** goes live only AFTER broker auth + API auth (else unauthenticated control).
- **Confirm-PIN store + admin API auth** (decision #6 above).

## Open / deferred (lower priority)
- Outdoor history read (`02` reject on attic/h_bed) — needs an app HCI-btsnoop of an attic/h_bed pull. LOW (ADR-0009).
- c_office meter **battery swap** (1–2%) — physical.
- **Aranet — DECODER + LIVE RELAY DONE & validated (2026-06-21).** Corrected to mfr 0x0702 ext-adv;
  `tools/aranet_relay.py` decodes + publishes canonical state (radon/temp/pressure/humidity/battery).
  Live-validated from `.112` (radon 10 Bq/m³). Registry MAC fixed locally (placeholder→F4:37:5A…).
  **DECIDE — where it scans from:** the relay needs a BT5/ext-adv-capable, *always-on* scanner IN RANGE
  of the device's final spot. `.112` works on the desk; the **crawlspace** needs its own scanner (the
  `.245` dongle couldn't reach it). Best full solution = a **Pi-class node** near the crawlspace (does
  BOTH live relay AND history pull, both Python). A C6 with ext-adv firmware could do live relay only.
  For now I can run the relay from `.112` on demand.
  - **History pull DONE** (`tools/aranet_history.py`): 30 days / 4320 records backfilled from the desk
    via the `aranet4` lib (GATT), idempotent. `aranet_radon` now on the dashboard. Re-run anytime in
    range to top up. ONGOING live data needs the relay running somewhere in range (placement decision).
  - **Deploy note:** `.245`'s registry also needs the real Aranet MAC if a scanner runs there.
- Retire `.112` duplicate services (`sudo systemctl disable --now ha-api ha-writer mosquitto`) — your sudo.
- G11 provisioning bring-up (arrives ~2026-06-23) — your hardware step + on-device LLM.
