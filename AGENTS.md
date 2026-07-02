# AGENTS.md — start here

Orientation for any agent/LLM working this repo. Read this, then the relevant subsystem `AGENTS.md`. Keep it
terse; it **routes** to the deep docs, it doesn't duplicate them. (Convention: ADR-0021.)

## What this system is

A self-hosted, **air-gapped home-automation system**. One node is the **dictator** — it runs the stack (MQTT
broker, ingest, storage, BFF/PWA, automation, notifications) and owns all authority (ADR-0001). Edge devices
are **dumb relays**: they sense/relay and take signed commands; they never hold policy. A warm-standby node
mirrors the dictator with keepalived/VRRP auto-failover behind a VIP.

- **Dictator:** currently `192.168.0.210` ("ha-dev"). **Warm standby:** `192.168.0.245`. **VIP:** `.200`.
- **⚠️ `.245` is Hugh's CRITICAL FILESERVER** and a temporary HA stand-in — **never** a dev/deploy/optimization
  or host-config target. Touch only its `ha-*` guest services, nothing else on the box.
- Deep reference: [home-automation-architecture-plan.md](home-automation-architecture-plan.md),
  [docs/ROADMAP.md](docs/ROADMAP.md), the [ADR index](docs/adr/).

## Directory map

| Dir | What's here | AGENTS |
|-----|-------------|--------|
| `server/` | The dictator stack: FastAPI BFF + PWA (`web/`), `ingest/`, `storage/` (sqlite hot + parquet archive), `control/`, `cluster/`, `notify/`, `weather/`, `mesh/` | [server/AGENTS.md](server/AGENTS.md) |
| `edge/` | ESP32 edge-node firmware (c3/c6/s3-eth) — BLE relay modules; `FIRMWARE-GUIDE.md` | [edge/AGENTS.md](edge/AGENTS.md) |
| `provisioning/` | Device/box recipes: server install, `reterminal/` panels, `levoit/`, `openwrt/`, `ntfy/` | [provisioning/AGENTS.md](provisioning/AGENTS.md) |
| `failover/` | Cluster HA: keepalived, reconcile (history/parquet), drill, cluster-doctor | [failover/AGENTS.md](failover/AGENTS.md) |
| `tools/` | Operator tooling: `agents/coord.py` (task board), `node_bringup`, `edge_ota`/`edge_sign`, `enroll_node` | [tools/AGENTS.md](tools/AGENTS.md) |
| `tests/` | `run_all.py` + suites guarding server logic | [tests/AGENTS.md](tests/AGENTS.md) |
| `docs/` | ADRs, decisions, retros, ROADMAP, CHECKPOINT, FOLLOWUPS | [docs/AGENTS.md](docs/AGENTS.md) |
| `systemd/` | `ha-*` unit files (the dictator services) | — |
| `config-examples/` | `*.example.yaml` templates (real configs live in `instance/`, git-ignored) | — |

Runbooks (how-to recipes): [SKILLS.md](SKILLS.md).

## Standing contracts (do not break)

- **Dumb-relay (ADR-0001):** edge nodes relay raw readings keyed by MAC; the **dictator owns the registry** and
  MAC→device/area mapping (`ha-edge-mapper`). Commands go *down* signed (ADR-0010).
- **Production writes are gated:** on the **live dictator**, restarting an *existing* `ha-*` service is fine;
  installing new packages/units or deploying new code is **gated → hand Hugh copy-paste, never self-deploy.**
- **Hugh runs box-side commands himself** — give direct on-box commands, not `ssh … 'bash -s'` wrappers.
- **Secrets never enter git/logs/transcripts.** Secret: MACs, GPS coords, the master passphrase, WiFi
  password, bearer tokens. Not secret: LAN IPs. Back up OEM/factory firmware **off-git**.
- **Auto-push:** in this repo, `git push` right after every commit (don't ask); verify `HEAD == origin/main`.
- **Checkpoint discipline:** at each checkpoint reconcile the action-item docs (FOLLOWUPS, ADR status, this
  tree) to reality — run [docs/CHECKPOINT.md](docs/CHECKPOINT.md), don't just commit code.

## Starting a task

1. Check the **coord board** — `python3 tools/agents/coord.py --as <ops|dev> list|ready|mine` (MQTT ledger on
   VIP `.200`; two-Claude coordination, see [tools/AGENTS.md](tools/AGENTS.md)).
2. Read the subsystem `AGENTS.md` for where you're working; follow its ADR links.
3. Honor the standing contracts above. At task end: commit+push, update FOLLOWUPS/board, checkpoint.
