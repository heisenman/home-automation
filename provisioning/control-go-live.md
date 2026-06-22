# Control plane go-live (runbook)

Turns the authenticated control API on. Prereqs already done: **broker auth/ACL cutover** (2026-06-21)
and **per-device enrollment + SHA-confirm** (encrypted LUT). This step mounts the control router into the
live API behind admin auth, with the PEP sourcing each node's command secret from the encrypted LUT.

## What's wired (code, committed)
- `server/api/main.py:_mount_control()` — mounts `/devices/{id}/command` **only if the master passphrase
  is available** (else read API runs unchanged — no unauthenticated control, ever). Degrades gracefully
  on any config error.
- **Admin auth** = `Authorization: Bearer <SHA256("ha-api:"+master)>` (server/control/secret_store.py
  `api_token`). Required on every control request. The **confirm token** (`SHA256("ha-confirm:"+master)`)
  stays the SEPARATE second factor for sensitive actions (e.g. unlock), sent as `confirm_pin` in the body.
- **Secrets from the LUT** — `registry.secrets_from_lut()` maps each control device to its owning node's
  `cmd_secret` from `instance/node_secrets.enc` (per-node HMAC key). Devices whose node isn't enrolled are
  simply uncommandable (logged at startup).
- `ha-api.service` carries `HA_MASTER_PASS_FILE=…/instance/.master_pass` + `EnvironmentFile=-…/instance/mqtt.env`
  (dictator broker creds for the issuer's MqttTransport).

## Deploy on .245 (supervised)
1. **Place the secret material** under `.245:~/home_automation/instance/` (gitignored — NOT shipped by git):
   - `.master_pass` (0600, the master `CHANGE_ME_master_passphrase`),
   - `node_secrets.enc` (the encrypted LUT — copy from the dev box),
   - `control.yaml` (actuator registry; may be empty `version: 1` until the first actuator is enrolled).
2. **Reinstall the unit + restart:** `sudo ./install.sh` (picks up the new ha-api.service env), then
   `sudo systemctl restart ha-api`.
3. **Verify** (replace MASTER):
   ```bash
   BEAR=$(python3 -c "import hashlib;print(hashlib.sha256(('ha-api:'+'<MASTER>').encode()).hexdigest())")
   journalctl -u ha-api --since "30 sec ago" | grep -i "control plane"      # expect: MOUNTED — N device(s)…
   curl -s -o /dev/null -w "%{http_code}\n" -XPOST localhost:8123/devices/x/command -d '{}'        # 401 (no bearer)
   curl -s -XPOST localhost:8123/devices/<dev>/command -H "Authorization: Bearer $BEAR" \
        -H 'Content-Type: application/json' -d '{"trait":"switchable","action":"set","args":{"on":true}}'
   ```
   With no actuator enrolled yet, a real device id returns `unknown-device` (404) or `no-ack` (504) — both
   prove auth+policy+signing ran. The read dashboard is unaffected.

## Status: server-side plane is COMPLETE; node-side actuator handling is the next gap
The control API publishes to `home/<area>/<device_id>/cmd` and awaits an ack on `…/cmd/ack`. No actuator
firmware consumes that yet (c6-bench handles only edge gatt/history/ota on `home/edge/c6-bench/cmd`). So:
- `control.yaml` stays minimal until a real actuator exists.
- First actuator = enroll its node (`tools/enroll_node.py`) → add a `control.yaml` device → add an ACL
  stanza for `home/<area>/<dev>/cmd[/ack]` → firmware that verifies the `{p,s}` HMAC and acks.

## Rollback
Remove `instance/.master_pass` (or unset the env) + `sudo systemctl restart ha-api` → control router
un-mounts, read API continues. Fully reversible.
