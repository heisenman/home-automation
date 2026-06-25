# Automatic failover — 210 (primary) ↔ .245 (standby) via keepalived/VRRP

**Goal:** if the primary dictator (210) dies, `.245` automatically takes over Midea control —
unattended — while **guaranteeing a single active `ha-controller`** as hard as 2 nodes allow.

> **Honest risk (read first):** VRRP elects MASTER from adverts on the LAN. On a true **network
> partition** (both boxes up, can't see each other) BOTH can become MASTER → two controllers → split-brain.
> We mitigate (peer-stop fencing in `notify_master`; both nodes on one switch makes a clean partition
> unlikely; the dominant failure = node-down, which VRRP handles cleanly). We do **not** fully eliminate it.
> If that residual risk is unacceptable, fall back to "auto-detect + alert, manual promote."

## Core rule — PRIMARY SUPREMACY (Hugh, 2026-06-24)
**210 is the permanent primary. `.245` is only ever a TEMPORARY stand-in — it can never be *permanently*
promoted without explicit user permission.** Two enforced behaviours:
1. **Auto-demote on primary return:** while acting as dictator, `.245` continuously watches for the original
   primary (210) being **healthily back** on the network; the moment it is (debounced ≥30 s to avoid flap),
   `.245` **stops its own controller and returns to standby** — 210 resumes as the sole dictator.
2. **Fixed role:** each box carries a static `role` (`primary` | `standby`). `.245`'s `standby` role makes
   its promotion a temporary fill-in *by definition*. Making `.245` the permanent primary (e.g. 210 is dead
   and replaced) is a deliberate, **user-permissioned** re-designation — never automatic.

**This is also our split-brain RESOLVER:** if a partition ever let both run, on reunite the standby *sees*
the primary and yields → single controller (210) → split-brain auto-heals, **deterministically in the
primary's favour**. The primary is the tiebreaker. (Shrinks the residual VRRP risk above to a brief
transient that self-corrects.)

## Runtime independence — NO LLM, NO GitHub in the loop (hard requirement, Hugh 2026-06-24)
The topology operates entirely on plain infrastructure: **keepalived (VRRP) + systemd + bash + the cluster
bus below.** No Claude/LLM is required at runtime — LLMs only author/maintain this code; nothing calls an
LLM to fail over, fence, or demote. No GitHub/internet is required either — everything is on the local LAN.
(GitHub is only where the *builder agents* coordinate; the running machines never touch it.)

## Cluster bus — out-of-band, direct node↔node RPC (local LAN, no GitHub)
Boxes coordinate directly using infra they already run. Three layers:
1. **MQTT heartbeat/events** — namespace `ha/cluster/#`, exchanged between brokers (separate namespace from
   device `home/#` → no telemetry-loop risk; bidirectional OK, or each node clients into the peer broker).
   Each node publishes `ha/cluster/<node>/heartbeat` every ~2–3 s: `{role, priority, controller_active,
   healthy, ts}`; peers subscribe. **This is how the standby senses the primary's return** (Core Rule
   auto-demote) — independent of, and redundant with, VRRP adverts.
2. **HTTP RPC on `ha-api`** (request/response, **bearer-authed** — a rogue LAN host must not be able to
   demote the dictator): `GET /cluster/status` (role/controller/health), `POST /cluster/demote` (stand
   down now), `POST /cluster/claim` (announce takeover). Explicit fencing + on-demand queries.
3. **SSH** — privileged ops: the actual `systemctl stop ha-controller`, token/policy file sync.

**Redundancy:** "primary is back" is sensed by BOTH VRRP adverts AND MQTT heartbeat; fencing stops the peer
via BOTH HTTP `/cluster/demote` AND SSH `systemctl stop`. The bus is reusable by future edge nodes for any
node→node signalling — not failover-specific. **Security:** cluster MQTT topics gated by broker ACL to
cluster identities; HTTP cluster routes require the admin bearer.

## Roles & addresses
- **210 = MASTER**, keepalived priority **150**. **.245 = BACKUP**, priority **100**.
- **VIP = 192.168.0.200** (the "dictator address" — *verify free before deploy*: `ping`/`arping`).
- **PREEMPT** (per Core Rule): a recovered 210 **reclaims** MASTER and `.245` **auto-demotes**. A
  `preempt_delay` + health debounce (210 must be *stably* back ≥30 s) prevents flapping on a flaky primary.

## The invariant & how VRRP enforces it
**Exactly one `ha-controller` runs = the VIP holder's.** keepalived `notify` scripts bind the controller
to VRRP state:
- `notify_master` → (1) **FENCE**: ssh the peer, `sudo systemctl stop ha-controller` (best-effort, 5s
  timeout — cleans the alive-but-not-master case); (2) ensure fresh `.master_pass` + `midea-device.env`
  present; (3) `sudo systemctl start ha-controller`.
- `notify_backup` / `notify_fault` → `sudo systemctl stop ha-controller` locally, immediately.
- `track_script` (every 5s) → health gate: controller alive + Midea reachable; failing it drops priority
  → triggers failover. Also a `vrrp_script` checking the local box isn't degraded.

Belt-and-suspenders (defends the partition case): the controller's actuation path can additionally refuse
to issue a Midea command unless it confirms it holds the VIP locally (`ip addr | grep 192.168.0.200`).
→ even if keepalived misbehaves, a non-VIP controller won't actuate. *(Phase 2 enhancement — see below.)*

## Warm-standby state sync (needed regardless of trigger mode)
`.245`'s standby must be current to take over well. A timer on `.245` pulls from 210 over **SSH (not git —
these are secrets)** every ~30 min:
- `instance/midea-device.env` (the Midea token **rotates ~18 h** — a stale standby can't drive the unit).
- the live **control policy** (`control.db` `automation_policy` + `override`, or `instance/control.yaml`).
- `.master_pass` + `node_secrets.enc` already on `.245` (it was dictator) — keep.

## Components (to build in `failover/`)
- `keepalived.245.conf`, `keepalived.210.conf` — the two VRRP configs (templated; real IPs via deploy).
- `notify.sh` — single notify script, dispatches on `$1` = master|backup|fault (fence + start/stop).
- `sync-standby.sh` — pull token + policy 210→.245 (runs from a systemd timer on .245).
- `reconcile-history.sh` — hot-tier (today's sqlite) bidirectional row-merge across boxes (ADR-0016).
- `reconcile-parquet.sh` — cold-tier parquet **archive** bidirectional row-merge keyed `device_id,ts,metric`
  (ADR-0018); `--once`/`--loop`/`--list`/`--merge`. The deep-reconcile ADR-0016 deferred.
- `provision-peer.sh` — bring a box up as a peer + **elevate to record-keeping** (config→hot→archive→HARD
  GATE); `--from <src>` `[--data-only]`. The 2026-06-25 archive-seeding gap, made a one-command gated step.
- `cluster-doctor.sh` — read-only cross-cluster invariant + completeness checker (config, hot convergence,
  **archive completeness**). Run after any failover / before trusting a box as dictator-of-record.
- `healthcheck.sh` — track_script body (controller + Midea reachable).
- `deploy.sh` — idempotent installer (place config + scripts, enable keepalived) — run per box.
- `failover-runbook.md` — operate/test/failback procedures.

## Prerequisites
1. **`.245`↔210 SSH keys** (key-based, both directions) — for fencing + sync. *First build step.*
2. **keepalived installed** on both (`sudo apt install keepalived` — needs Hugh's password on `.245`).
3. NOPASSWD already covers `systemctl start/stop ha-*` on `.245`; notify scripts rely on that.

## Build + TEST plan (incremental, never blind-deploy on the live dictator)
1. SSH keys `.245`↔210; verify fence command works both ways (read-only test).
2. Author configs/scripts in `failover/`; **dry-run** the notify + healthcheck logic by hand.
3. Deploy keepalived on **.245 (BACKUP) first**, 210 priority unset → confirm `.245` stays BACKUP, controller
   stays OFF (no takeover while 210 healthy). VIP sits on 210.
4. Bring keepalived up on 210 (MASTER) → VIP lands on 210; confirm only 210's controller runs.
5. **Controlled failover test:** stop keepalived on 210 → `.245` should take VIP, fence, start its
   controller; verify **exactly one** controller + Midea continuity. Then **failback** (manual) to 210.
6. Wire `sync-standby.sh` timer; verify token/policy land on `.245`.
7. (Phase 2) Add the VIP-ownership check to the controller's actuation path for the partition fence.

## Coordination (210-side owns its half)
- 210: install keepalived, place `keepalived.210.conf` + `notify.sh`, expose policy/token for `.245`'s
  pull (or accept .245's read-only pull via SSH key), confirm its `notify_backup`/`fault` stops its
  controller. 210 is the LIVE box — changes there must not disrupt current control; test with care.
- `.245`-side (me): everything on `.245` + the shared scripts/design here.
- Status via the per-side files (`docs/cutover/{245,210}-status.md`); Hugh gates the first real failover test.
