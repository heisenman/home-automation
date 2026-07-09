# AGENTS.md — start here

Orientation for any agent/LLM working this repo. Read this, then the relevant subsystem `AGENTS.md`. Keep it
terse; it **routes** to the deep docs, it doesn't duplicate them. (Conventions: ADR-0021, ADR-0025.)

## Principles (non-negotiable — read before you build)

These five govern all work here; they are first on purpose. Detail lives in the ADRs they cite.

1. **Reuse-first.** Before building anything, find what already exists: [`docs/REUSE.md`](docs/REUSE.md) (the
   capability catalog), the ADR nearest your task, `firmware/components/`, existing `cmd/*` handlers, and
   `tools/`. **Reuse it, or justify in your commit why not.** ([ADR-0025](docs/adr/ADR-0025-reuse-first-navigation.md)
   — this repo has *already* nearly rebuilt capabilities that existed; that is the failure this rule exists to stop.)
2. **Docs-first.** Before asserting any knowable fact (a pin, register, wire format, API contract), read the
   authoritative doc — schematic, datasheet, spec, ADR. Recall or probing may *confirm* a doc; it never *replaces* one.
3. **Decompose / module-first.** New capability = a new module, named in a decomposition step *before* code —
   no god-files, no `cp -r` forks. ([ADR-0020](docs/adr/ADR-0020-shared-edge-panel-firmware-core.md); firmware
   detail in [firmware/AGENTS.md](firmware/AGENTS.md).)
4. **Conform by ability.** A device's obligations follow from *what it does*, not what it's called. Accepted
   ADRs are **normative directives**, not just design records. Before building or onboarding a device — or
   adding an ability to an existing one — **DO** enumerate its abilities, **CHECK** each against
   [`docs/CONFORMANCE.md`](docs/CONFORMANCE.md) (the ability→ADR conformance catalog), and **ADHERE** to every
   SHALL it binds. It isn't done until it conforms; a forgotten device-registration can't excuse a missed
   contract.
5. **Trust but verify (device state).** A device's state or function is a *hypothesis* until **verified live** —
   documented, remembered, or briefly-sampled state is not a fact (a short sample can miss a slow update cadence).
   Verify before you rely on it in planning or execution, **even if it costs more time and tokens.** Any doc that
   asserts a device's state/stance **SHALL** carry a companion reference to how to re-check it, kept in
   [`docs/runbook-device-verification.md`](docs/runbook-device-verification.md) — verification procedures live there
   (durable + separate), never inline in the churning design log. ([ADR-0010](docs/adr/ADR-0010-command-control-protocol.md)
   pairs with this: signed control assumes the device is what the registry says — so confirm what it *does*.)

**If your task writes or changes code, take the reuse self-test in [ADR-0025](docs/adr/ADR-0025-reuse-first-navigation.md)
— honestly, before you build.** If you cannot say *"yes, I'd discover and reuse before building,"* you are the
reason this section is first: open [`docs/REUSE.md`](docs/REUSE.md) now.

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
- **Air-gap migration:** [docs/airgap/AIRGAP-MIGRATION.md](docs/airgap/AIRGAP-MIGRATION.md) — requirements +
  final architecture + learnings/whoopsies (read before touching air-gap devices); the running journal is
  [docs/airgap/MIGRATION-DESIGN-LOG.md](docs/airgap/MIGRATION-DESIGN-LOG.md).
- **Secrets:** [docs/SECRETS.md](docs/SECRETS.md) — the discovery index (where each secret class lives). Don't
  hunt; look here first.

## Machines & repo checkouts — READ BEFORE BUILDING/FLASHING

**Three machines, one GitHub origin (`heisenman/home-automation`), but the checkout folder name DIFFERS per
machine** — this has caused real confusion. Full detail + the per-device build/flash matrix:
**[docs/ENVIRONMENT.md](docs/ENVIRONMENT.md).** The short version:

| Machine | Addr | Checkout | Role | Builds/flashes here | Edge `secrets.h`? |
|---------|------|----------|------|---------------------|-------------------|
| **bench** (`SuperDuperBuddy`) | `.112` | `~/ha-coord` | **ops** — the "convenient bench" (Claude ops sessions); ESP-IDF present | **reTerminal panels** (P4/C6; dev tree `~/reterminal-dev`) | **NO** — only `secrets.example.h` |
| **ha-dev** (dictator) | `.210` | `~/home_automation` | **canonical real dev** (Hugh is moving all real dev here) + the live dictator; ESP-IDF present | **edge nodes** (c3/c6/s3): build + **signed OTA** | **YES** — real per-node secrets |
| **fileserver** | `.245` | — | CRITICAL fileserver + warm standby | nothing — **hands-off** | — |

**Rule of thumb:** panel firmware → **bench**; edge-node firmware (build, sign, OTA) → **`.210`** (that's where
`secrets.h` + the command secrets live). Never build/OTA an edge node from the bench — its `secrets.h` isn't
here, so the bin gets empty wifi/command-secret and can't reconnect or be signed. To tell where you are:
`hostname` / `pwd` (folder name) / `hostname -I`.

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

Runbooks (how-to recipes): [SKILLS.md](SKILLS.md). Bringing in a new device (capability survey → conformance →
reuse → decompose → build → test): [docs/DEVICE-INTAKE.md](docs/DEVICE-INTAKE.md).

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

1. **Reuse-first (if you'll write code):** scan [`docs/REUSE.md`](docs/REUSE.md) + the ADR nearest your task +
   `firmware/components/` for prior art — **reuse or justify** (Principle 1). Don't build until you've looked.
2. **Conform by ability (if you'll build/onboard a device):** DO enumerate what it does; CHECK each ability in
   [`docs/CONFORMANCE.md`](docs/CONFORMANCE.md); ADHERE to every SHALL it binds (Principle 4).
3. Check the **coord board** — `python3 tools/agents/coord.py --as <ops|dev> list|ready|mine` (MQTT ledger on
   VIP `.200`; two-Claude coordination, see [tools/AGENTS.md](tools/AGENTS.md)).
4. Read the subsystem `AGENTS.md` for where you're working; follow its ADR links.
5. Honor the standing contracts above. At task end: commit+push, update FOLLOWUPS/board, checkpoint.
