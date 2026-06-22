# Broker auth + ACL cutover (runbook)

**Status:** staged, NOT executed. Flips the broker from anonymous to authenticated + topic-ACL'd
(ADR-0001 §13.4, ADR-0010). This is a **coordinated cutover**: every client must hold credentials
*before* `allow_anonymous` is turned off, or it is locked out. Do this supervised, with the C6
recable option on hand.

Config artifacts (in `server/config/`): `acl`, `mosquitto-auth.conf`.

## Identities
- `dictator` — the server: PEP issuer + ingestion services (scanner, writer, edge-mapper, edge-history,
  api, weather) + admin tools. Full pub/sub.
- `c6-bench` — the edge node: publishes only its own telemetry; subscribes only its own `cmd`; may NOT
  publish any `…/cmd`.
- (future) one identity per actuator node, same shape.

## Code state (done 2026-06-21 — the prerequisite that was missing)
Every server MQTT client now reads `$HA_MQTT_USER`/`$HA_MQTT_PASS` via
`server/util/mqtt_creds.py:apply_credentials()` — wired into scanner, writer, edge-mapper, edge-history,
and the PEP issuer's `MqttTransport`. Unset → anonymous (today). So "give services creds" is now just an
env-file drop, not a code change. The three live units already carry
`EnvironmentFile=-.../instance/mqtt.env` (optional via `-`, gitignored), template at
`server/config/mqtt.env.example`.

## Order of operations (each step reversible until the last)
1. **Node carries broker creds FIRST.** Add `HA_MQTT_USER="c6-bench"` + `HA_MQTT_PASS=…` to the C6
   `secrets.h`, set them on the esp_mqtt_client_config, build, **OTA**. Confirm the node reconnects
   (still anonymous broker — creds simply ignored until step 5). *This is the prerequisite flash.*
   *(c6-bench creds already compiled into the deployed firmware as latent creds — 2026-06-21.)*
2. **Create the password file** on .245:
   `sudo mosquitto_passwd -c /etc/mosquitto/passwd dictator` then `sudo mosquitto_passwd /etc/mosquitto/passwd c6-bench`
   (use the SAME c6-bench password that is in the node's `secrets.h` / LUT).
3. **Install the ACL:** `sudo cp server/config/acl /etc/mosquitto/acl`
4. **Give every server service the `dictator` creds:** copy the env template and set the password —
   `cp server/config/mqtt.env.example instance/mqtt.env`, edit `HA_MQTT_PASS` to the `dictator` password
   from step 2, `chmod 600 instance/mqtt.env`. Then `sudo systemctl restart ha-scanner ha-writer
   ha-edge-mapper` (units already reference the file). They still work (broker still anonymous), creds
   now latent-loaded. Verify each logs `MQTT connected` after restart.
5. **Flip auth:** `sudo cp server/config/mosquitto-auth.conf /etc/mosquitto/conf.d/homeauto.conf`
   (replacing the anonymous one) → `sudo systemctl reload mosquitto`.
6. **Verify:** all services reconnect; C6 status returns `online`; a signed gatt probe works; an
   anonymous `mosquitto_pub` to any topic is now refused.

## Rollback
Restore the anonymous `homeauto.conf` and `sudo systemctl reload mosquitto`. (Steps 1–4 are harmless
if left in place — latent creds on an anonymous broker.)

## Note
Command authenticity is already enforced **end-to-end** by the per-device HMAC signature (ADR-0010,
live since 2026-06-21) — even on today's anonymous broker a forged command is refused at the node.
This cutover adds the *channel* defense (only the dictator can publish commands; passive-sniff
resistance via creds, later TLS), i.e. defense in depth, not the sole gate.
