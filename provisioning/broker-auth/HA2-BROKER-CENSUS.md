# ha-2 PROD broker — client census (for broker-auth-rescope, air-gap side)

> **DECISION 2026-07-10 (Hugh): NOT proceeding. `broker-auth-rescope` KILLED. ha-2 stays anon.**
> Marginal gain (telemetry-injection only; actuation already HMAC-gated) vs. a high-effort, all-or-nothing,
> fleet-wide cred campaign — and the system already carries enough deferred fleet-touching risk (device-admin
> UI, ADR-0034 taxonomy not yet on prod, module-refactor OTA). Air-gap isolation (ADR-0031 "trusted air-gap
> LAN") is taken to satisfy the "air-gapped PROD = ENABLED" intent. This census is retained as reference in
> case the posture is revisited. See `docs/decisions/broker-auth-posture.md`.


Compiled **2026-07-10 by the interactive session (seat dev4)** directly from ha-2 (`192.168.1.210`) over the
air-gap leg — the census the `.210`-side `CLUSTER-BROKER-INVENTORY.md` explicitly *could not* do
("dev-reported; not inspected from .210"). Read-only: `ssh` + `ss` + mosquitto-log grep. **No writes to prod.**

Per the revised posture (Hugh 2026-07-10, `auth-posture-by-network-context`): internet=REQUIRED, **dev=OPTIONAL**,
**air-gapped-PROD=ENABLED**. That makes **this broker (#3), not .210 (#1), the real auth target.** This file is
the client census that authing it requires. **No secrets here** — client identities by name only.

## ha-2 broker auth posture NOW
- `/etc/mosquitto/conf.d/homeauto.conf`: `listener 1883 0.0.0.0`, **`allow_anonymous true`**, no `password_file`,
  no `acl_file`. Pure anon. (Confirmed via SSH; matches dev's prior second-hand report.)

## Client set that breaks the moment ha-2 flips anon→authed
Snapshot: `ss` established conns to `:1883` + client-ids from `mosquitto.log`. **Three classes, three different
cred-provisioning mechanisms** — this is the cost driver.

### A. Local `ha-*` services (127.0.0.1) — ~13, cheap (one server identity via env)
`ha-api`, `ha-controller`, `ha-writer`, `ha-scanner`, `ha-cluster-heartbeat`, `ha-cluster-watch`,
`ha-edge-history`, `ha-edge-mapper`, `ha-reconcile-history`, `ha-relay-alert-egress`, `ha-levoit-bridge`,
`ha-tasmota-bridge`, **`ha-fence-listener`** (note: fence IS deployed on ha-2). Most connect with `auto-*`
client-ids; two are named (`ha-cluster-210`, `relay-alert-egress-in`). Fix = a `dictator`/server identity in
`instance/mqtt.env` (mirrors the proven standby scheme). Low friction — server-side only.

### B. Air-gap EDGE DEVICES (192.168.1.1xx) — ~11, EXPENSIVE (per-device cred into firmware/config)
| client-id | IP | class | cred mechanism |
|---|---|---|---|
| `d1001-beachhead` | .110 | reTerminal panel (native) | node_secrets / panel config — reflash-or-repoint |
| `e1001-c-office-a4cb8fcf46e8` | .131 | E1001 panel (ESPHome) | compiled `secrets.yaml` → OTA |
| `levoit-office-dc1ed53d34d0` | .148 | Levoit (ESPHome) | compiled `secrets.yaml` → OTA |
| `DVES_40C8C0` | .115 | Tasmota | `SetOption`/console MQTT user+pass |
| `DVES_40D006` | .121 | Tasmota | Tasmota MQTT creds |
| `DVES_40D1EF` | .103 | Tasmota | Tasmota MQTT creds |
| `ESP32_1aE6BC` | .105 | native edge (C6/S3) | enroll flow / `node_secrets.enc` |
| `ESP32_54ABE0` | .126 | native edge | enroll flow |
| `ESP32_85B414` | .139 | native edge | enroll flow |
| `ESP32_85B7F8` | .127 | native edge | enroll flow |
| `ESP32_a0FF34` | .125 | native edge | enroll flow |

MAC-tail ids don't resolve against `.210`'s registry — the authoritative device registry lives **on ha-2** now
(migrated record-of-record). Name them from ha-2's `instance/devices.yaml` before the cutover.

### C. The `.210`/dev straddle bridging INTO ha-2 (192.168.1.245, ~10 conns) — gray, joint scope w/ dev2
This box (`.245` = .210's air-gap leg) holds ~10 `auto-*` client connections into ha-2 — cluster bridge +
scanner/tasmota relays + coord/failover reach. These are the ADR-0031 dev-convenience straddle (`airgap-standby-is-dev-convenience`). Each needs creds too, OR is dropped when the straddle is retired.

## The finding that changes the decision
Authing ha-2 is **not** a server-side env change — it is a **fleet-wide credential campaign touching ~11
production devices across 3 firmware/cred mechanisms** (native-enroll, ESPHome-compiled, Tasmota-console), each
requiring a device touch (OTA / repoint / console), all deployed to **air-gapped ha-2 via scp with the
deploy-drift hazard** (`airgap-checkout-drift`), and **all-or-nothing** (every un-cred'd client drops at flip).

Marginal security gain is **telemetry-injection protection only** — actuation is already HMAC-gated per-device
(ADR-0010), so control is safe regardless of broker auth. On an **already air-gapped** network (ADR-0031
"trusted air-gap LAN"), that gain is smaller than the campaign's risk/cost. **This is the tradeoff for Hugh:**
the revised posture says "air-gapped PROD = ENABLED," but the network isolation may already satisfy its intent.

## Recommended sequencing IF we proceed
1. **Server identity first** (class A) — `dictator` cred + `instance/mqtt.env` on ha-2; leaves `allow_anonymous
   true` so nothing drops yet. Zero-downtime staging.
2. **Per-device creds** (class B) — provision during a **device-touch window** (fold into the next OTA/repoint
   campaign, not a standalone flip); name each from ha-2's registry first.
3. **Straddle** (class C) — decide with dev2 whether to cred or retire the .245 bridge.
4. **Flip `allow_anonymous false` LAST**, on ha-2, with a dead-man revert (mirror the failover-drill
   trap/restore pattern) — only after A+B+C are cred-staged and verified live on ha-2.

Do **not** flip .210 (#1) — per revised posture it's OPTIONAL and it's the one that kept auto-reverting.
