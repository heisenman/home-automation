# Cluster broker + `ha/cluster/#` client inventory (for broker-auth re-scope)

Compiled by the **dev2 wake-runner** 2026-07-10 on request from dev (broker-auth cutover auto-reverted; the
first census missed credless cluster-bus clients — commit de0c0ea). This is the read-only inventory dev asked
for: **every broker and every `ha/cluster/#` client that must be cred-staged BEFORE any anon→auth flip.**

Facts only — no posture decisions (those stay with the interactive session / Hugh; see open questions at end).
**No secrets in this file.** Bridge creds live in the gitignored `failover/mosquitto/cluster-bridge.conf` and
`/etc/mosquitto/passwd` (hashed); referenced by identity name only.

## Brokers (3)

| # | Broker | Unit / config | Listeners | Auth now | Scope |
|---|--------|---------------|-----------|----------|-------|
| 1 | **Dev/dictator** (.210 household+dev) | `mosquitto.service` → `/etc/mosquitto/conf.d/homeauto.conf` | `127.0.0.1:1883`, `192.168.0.210:1883`, `192.168.0.200:1883` (household VIP) | **anon** (reverted to known-good) | main repo — the one dev's cutover targeted |
| 2 | **Air-gap standby** (box-local, ADR-0031 Pillar 2) | `ha-ag-mosquitto.service` → `~/ha-airgap-standby/instance/mosquitto-airgap.conf` | `192.168.1.245:1883`, `192.168.1.200:1883` (air-gap VIP, nonlocal-bind) | **anon** ("trusted air-gap LAN" stance) | standby tree, NOT in main repo |
| 3 | **ha-2 prod** (separate host) | (on ha-2) | — | **anon** (dev-reported; not inspected from .210) | prod record-of-record |

Note the two `…200` VIPs are **different**: `.0.200` = household VIP on broker #1; `.1.200` = air-gap VIP on
broker #2.

## `ha/cluster/#` bridges (mirror heartbeats between dictator nodes)

- **`cluster-245-to-210`** — `~/ha-airgap-standby/failover/mosquitto/cluster-bridge.conf`. OUTBOUND from the
  standby/.245 broker → `192.168.0.210:1883`, `topic ha/cluster/# both`. **Already carries `remote_username
  cluster245` + password** → this bridge is *already cred-ready* for broker #1's auth **iff** the `cluster245`
  entry in `.210`'s `/etc/mosquitto/passwd` matches. (Could not read passwd w/o sudo this run — verify the
  entry survives / is re-created on re-flip.)
- The `.210` README claims **two** `cluster245` bridges (the `.245` fileserver bridge AND the box-local
  standby broker). **Confirm on the `.245` host** whether a second `/etc/mosquitto/conf.d/cluster-bridge.conf`
  exists there and what remote it points at — not verifiable from .210.

## `ha/cluster/#` clients (these break silently on an anon→auth flip)

**Long-lived services (send creds via env — likely OK):**
- `server/cluster/heartbeat.py` (`ha-cluster-heartbeat.service`) — publishes `ha/cluster/<node>/heartbeat`;
  `HA_BROKER=localhost` + `EnvironmentFile=instance/mqtt.env` → sends `dictator` user. Present in BOTH main +
  standby trees.
- `server/cluster/node_watch.py` (`ha-cluster-watch.service`) — subscribes `ha/cluster/+/heartbeat`; standby
  variant sets `HA_BROKER=192.168.1.210` + `instance/mqtt.env`.

**⚠️ Anon clients that were the census-missers — MUST get creds before the flip:**
- `tools/agents/coord.py` — the agent-RPC / coord bus. `BROKER=192.168.0.200` (household VIP = broker #1),
  raw `mosquitto_pub`/`mosquitto_sub` with **no `-u/-P`** → anon. This is the "dev2 `ha/cluster/#` coord"
  client dev flagged. Auth'ing broker #1 kills coord unless it passes creds.
- `failover/primary-watch.sh`, `failover/cluster-doctor.sh`, `failover/failover-drill.sh` — all
  `mosquitto_sub -h $BROKER -t ha/cluster/<node>/heartbeat` with **no creds**. Present in both trees. These
  are the failover-sensing CLI clients; anon auth-flip = they read empty = false "peer down".

**Future client (deploy pending — scope together):**
- `failover/cluster-ssh/fence.py` — fence-over-bus on `ha/cluster/fence`, connects `FENCE_BROKER=127.0.0.1:1883`
  (broker #1). Today it's **anon + HMAC-authorized** (`instance/.fence_key`), and NOT yet deployed. When
  brokers go authed it needs broker creds too — dev is right to scope broker-auth + fence together.

## Why the first cutover missed clients

The `.210` README census counted only "local `ha-*` services + two cluster bridges." It missed: (a) the
**CLI `mosquitto_sub` failover scripts**, (b) **`coord.py`** (VIP, anon), and (c) the **standby broker's own
outbound bridge** as a client of #1. Any re-scope must cred **all** of the above and stage them BEFORE
flipping, with an explicit (non-racy) revert path.

## Open posture questions — NOT decided here (for interactive session / Hugh)

1. **ha-2 broker (#3):** auth it too, or leave anon? It's the prod record-of-record; edge devices hit the
   air-gap VIP not ha-2 directly — needs the same client census on ha-2 before deciding.
2. **Air-gap standby broker (#2):** auth it, or is "trusted air-gap LAN" (ADR-0031) sufficient to leave anon?
   Auth'ing it means cred'ing its bridge + all local air-gap clients and risks failover heartbeats if botched.
3. **Sequencing:** flip brokers independently or together? Bridges/coord span both `.0.200` and the air-gap
   leg, so a half-flip can strand `ha/cluster/#` mirroring.
