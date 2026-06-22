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

## Gotchas hit during the live cutover (2026-06-21 — all resolved)
1. **passwd file unreadable by the broker → EVERYONE rejected.** `mosquitto_passwd -c` writes
   `/etc/mosquitto/passwd` as `root:root 0600`; mosquitto reads it *after* dropping to the `mosquitto`
   user, so it can't open the file and returns `Not authorized` for **every** client — even with the
   correct password (the broker log shows all clients "disconnected, not authorised", incl. the ESP32).
   Fix: `sudo chown mosquitto:root /etc/mosquitto/passwd && sudo chmod 0640` then `reload`. Owner =
   daemon (can read); group = root (silences mosquitto_passwd's "group is not root" warning). Same read
   requirement applies to `/etc/mosquitto/acl`.
2. **`instance/mqtt.env` left at `CHANGE_ME` → false-positive at step 4.** On the still-anonymous broker
   the bad password is *ignored*, so services log `MQTT connected` and step 4 looks healthy — then they
   all fail `Not authorized` the instant auth is flipped. ALWAYS set the real `HA_MQTT_PASS` (and verify
   with an authenticated `mosquitto_pub`) BEFORE the flip, not just "services started".
3. **Interactive `mosquitto_passwd` typos.** Set the broker entry from the SAME source as the client,
   non-interactively: `sudo mosquitto_passwd -b /etc/mosquitto/passwd <user> '<pass>'`. For a node, use
   the exact `secrets.h`/LUT value (e.g. c6-bench = `pwrSQTzdmLUUo7diJEb9V3do`). The dictator pass can be
   generated once (`openssl rand -base64 18`) and written to both the passwd file and `instance/mqtt.env`
   from one shell variable so they cannot drift.

## Note
Command authenticity is already enforced **end-to-end** by the per-device HMAC signature (ADR-0010,
live since 2026-06-21) — even on today's anonymous broker a forged command is refused at the node.
This cutover adds the *channel* defense (only the dictator can publish commands; passive-sniff
resistance via creds, later TLS), i.e. defense in depth, not the sole gate.
