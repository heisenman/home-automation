# Follow-ups & clarifications for Hugh

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
- Aranet radon live capture — needs ext-advertising scan firmware + a node near the crawlspace.
- Retire `.112` duplicate services (`sudo systemctl disable --now ha-api ha-writer mosquitto`) — your sudo.
- G11 provisioning bring-up (arrives ~2026-06-23) — your hardware step + on-device LLM.
