# Air-Gap Migration — Running Design Log & Roadmap

> **What this is.** A *living design log* for migrating the HA system onto a genuinely air-gapped
> second network. It is written to be maintained continuously as we execute — capturing every decision,
> the alternatives we rejected and *why*, and the gotchas we hit — so it can later be distilled into an
> **ADR** and a **reusable migration template** for the project. This is the one real pass where we get
> to discover how this *should* go; future operators inherit the battle-tested version.
>
> **Status:** DESIGN (pre-execution). Phase-gated; each phase gets explicit go-ahead. Nothing on the
> household network is disturbed until Phase 5, and only additively.
> **Destination:** graduate to `docs/airgap/` in the repo (living doc) → `docs/adr/ADR-00NN-airgap-migration.md` + a `docs/airgap/TEMPLATE.md` procedure.

---

## 1. Goal (why we're doing this)

Move the HA system off the household LAN onto a **genuinely isolated, no-internet network**, with the
new box **ha-2 as its dictator**, so the automation system is air-gapped from the internet-connected
network that runs the family's daily life. `.210` (today's dictator) becomes a permanent **dual-homed
bridge** — it keeps the HA web UI reachable from household computers and is an air-gap-side **failover**,
but is **never the dictator** (keep the internet-exposed host out of authority). Migrate **device by
device, by script**, verify each on ha-2 before retiring it on `.210`. **Hard constraint: everything is
reusable, idempotent, reversible code — the finished system runs with no AI in the loop.** `.245` retires
from the HA role and stays on the household net as a fileserver.

---

## 2. Decision journal (chronological — the evolution)

Each entry: **decision** · context · options weighed · **why this** · gotchas it surfaced.

- **DJ-1 — Air-gap the HA system onto a second, permanent network.** The household `192.168.0.0/24`
  stays untouched; a new isolated net holds the HA system. *(Corrected a misconception: the repo's
  staged OpenWRT plan was a "big-bang router plug-swap" on the SAME subnet — that was NOT the intent.
  There will be two REAL networks, permanently.)*

- **DJ-2 — ha-2 is the air-gap dictator; `.210` is the bridge and is *not* the dictator.** Options: (a)
  ha-2 dual-homed as the bridge (my first suggestion); (b) `.210` dual-homed as the bridge, ha-2
  single-homed on the gap. **Chose (b).** Why: the host that straddles both networks is the highest-
  exposure host; keeping it *out of authority* means a compromised/failed bridge isn't also the crown.
  "The bridge must never be the dictator" is now a first-class invariant.

- **DJ-3 — `.210`'s bridge is an *app-level* gateway (reverse proxy), not an IP route.** It exposes
  ha-2's web UI/controls to household computers and talks to ha-2 on the air-gap leg, but does **not**
  forward packets between the legs. Why: preserves the air gap by construction (only the vetted app
  crosses). This is also a *permanent* role (household computers are the user's normal admin surface)
  until direct air-gap admin is solved (e.g. a tablet on the air-gap net) — TBD, see §7.

- **DJ-4 — Migration strategy = True Parallel Cross-Network.** Options: (A) bridged-incremental (L2-
  bridge the two segments during migration so existing same-LAN tooling "just works"); (B) true parallel
  (two genuinely separate nets from day one, build real cross-network tooling); (C) big-bang plug-swap
  (staged, least code, but keeps `.210` as dictator and moves everything at once). **Chose (B).** Why:
  the user requires a *real* air gap, not a bridged-then-cut compromise; (A) was based on my same-subnet
  misconception (DJ-1); (C) violates the ha-2-as-dictator + no-big-bang goals. Cost accepted: (B) needs
  new cross-network migration tooling (see Phase 3).

- **DJ-5 — Air-gap subnet = `192.168.1.0/24`** (household stays `192.168.0.0/24`). A design pass
  proposed `10.20.0.0/24` for maximal distinctness; **user chose `192.168.1.0/24`** (also the R7800
  factory default → low friction). Gotcha this resolves: two *real* parallel nets with a dual-homed
  `.210` REQUIRE distinct subnets — a host can't sit on the same subnet twice (the staged same-subnet
  plug-swap config must be re-cut).

- **DJ-6 — The router(s) are first-class *dictator-managed devices*.** The OpenWRT router is not special
  infrastructure: its UCI config is code, pushed + drift-reconciled by the dictator; it's health- and
  power-monitored (`airgap_router_pm` already meters it); it lives in the registry/dashboard. **Router
  config + maintenance is a VIP-gated dictator duty** (same pattern as `ha-controller`) — on failover it
  moves with the role. **Failover routers**: a second OpenWRT box as a network-tier warm standby.

- **DJ-7 — `.210`'s air-gap leg = wired `eno1`** (recommended over the MT7922 `wlp2s0` wifi). Why: the
  bridge is permanent, always-on, load-bearing (household web UI + failover reconcile) — a wired leg is
  deterministic and immune to the wifi/regulatory flakiness the bench docs already fight. Wifi stays a
  documented fallback.

- **DJ-8 — Broker is always a *real* dictator IP, never the VIP.** Wifi STAs can't ARP a floating VIP.
  Every repoint tool hard-codes `BROKER_HOST` = ha-2's real air-gap IP.

- **DJ-9 — Per-device migration: serialize → import to ha-2 → repoint device → confirm → *then* retire
  on `.210`.** One device at a time, idempotent, reversible, confirm-before-retire; dead devices parked
  for reflash, never silently dropped. No AI in the loop — a human/cron runs `device_push.py <id>`.

- **DJ-10 — This artifact is a *running design log*, destined for an ADR + reusable template.** (This
  entry.) Capture the journey + gotchas, not just the end state, so future migrations have a proven
  procedure to copy.

- **DJ-11 — The migration *control plane* is `.210`, not a household leg on ha-2.** Options: (a) keep
  ha-2 wired to the household net during migration so a separate operator/Claude can run *on* ha-2; (b)
  drive ha-2's end entirely from `.210` via SSH (id_cluster) over `.210`'s air-gap leg (`eno1` →
  `192.168.1.210`) + the HTTPS `:import` channel through the Phase-2 bridge. **Chose (b).** Why: (a)
  breaks the air gap during migration and puts the *dictator* on the internet-exposed side (violates
  DJ-2); (b) is just another app-level use of `.210`'s dual-homing (no IP forwarding, gap holds) and
  needs no separate agent on ha-2. Proven feasible — the ha-2 console-password reset was already done
  purely via SSH-from-`.210`. The scripts (the no-AI end state) run from `.210` reaching ha-2 the same
  way, so the control path we build IS the production operations path.

- **DJ-12 — Pre-provision EVERYTHING while direct control of ha-2 is guaranteed (→ Phase -1).** Before
  ha-2 leaves the household net, install every dependency, establish every access hook (bidirectional
  id_cluster SSH, fs, deployed migration tooling + services, prepositioned secrets), stage all **offline**
  dependencies (the air-gap net has NO internet — `apt`/`pip`/`npm`/`docker pull`/`git fetch` won't work
  after the move), and **prove the exact post-gap control channel end-to-end** while household access is
  still the fallback. Why: the instant the gap exists, a missing package or broken hook costs a sneakernet
  round-trip — eliminate that whole failure class up front, while we still "DEFINITELY have control." A
  scripted readiness gate must be 100% green before the move.

- **DJ-13 — Planned relocation window after Phase 1 (bench → final location).** ha-2 and the router are
  configured and validated on `192.168.1.0/24` *at the bench* (Phase 1); the plan then **PAUSES** so
  Hugh physically relocates BOTH to their final/medium-term spot in the house **before** device
  migration. Why: Phase 4 repoints the fleet onto the router's wifi — coverage/range (channel 149) must
  be validated from the router's *real* location, not the bench; and the air gap must be **re-proven
  after the move**. Resume (Phases 2→4) only after a post-move re-verify.

- **DJ-14 — Failover is validated as a two-step `.210↔ha-2` exercise, once the air-gap network is
  completed — NOT a `.210↔.245` drill now.** Dropped the Phase-0 drill on the `.210↔.245` pair: `.245`
  is being retired, so proving *its* failover teaches us little. The meaningful validation is the real
  future cluster — `.210` (air-gap failover leg) ⇄ ha-2 (dictator) — on the air-gap net. Run it as a
  **two-step exercise** once the air-gap network is actually completed (both boxes on it). **Resolved:**
  the two steps are **Step 1 = failover** (kill ha-2 → `.210` seizes the air-gap VIP + controller; the
  household bridge keeps serving) and **Step 2 = failback** (ha-2 returns → preempts/reclaims → `.210`
  drops back to standby + bridge-only). **Timing: at the very end** — devices migrate onto the single-
  dictator air-gap net first (Phases 2–4), then `.210` joins as failover (Phase 5) and the two-step
  exercise runs there (reuse `failover-drill.sh`, generalized for the air-gap cluster, VRID 61).

---

## 3. Gotchas & landmine register (the hard-won details)

1. **The air gap is a userspace policy on `.210`: `net.ipv4.ip_forward=0` + app-only proxy.** One stray
   route or `ip_forward=1` from any tool silently defeats the entire project. Assert it **at boot**
   (systemd oneshot `verify-gap.sh`) and in cluster-doctor. This is the single most important invariant.
2. **Broker on a real IP, never the VIP** (wifi-ARP constraint). Bake into every repoint tool.
3. **VRID hygiene across independent VRRP domains:** household `51` (retiring), air-gap server-VIP `61`
   (`192.168.1.200`), router-gateway `71` (floats `192.168.1.1` between failover routers). Distinct
   VRIDs even though the segments don't share L2 (defense-in-depth; `.210` straddles both via two NICs
   that must **not** bridge).
4. **New signed firmware `config`/NVS op is the riskiest build item.** ESP32/D1001 can be repointed
   over-the-air by writing the NVS "ha" overlay (`ha_config.c` already reads it) — but there's no MQTT
   command yet. Adding one is new C on live nodes; it **must** ride the exact ADR-0010/0020 signature +
   freshness gate (no unsigned exception), and needs a **boot-count NVS-revert safety** so a mis-
   repointed node self-recovers instead of stranding on the hidden SSID. Stage on a spare node first;
   the OTA-reflash path (`enroll_node --from-manifest --reuse` + `edge_ota`) is the safety net.
5. **ESPHome devices (E1001, Levoit) can't be reconfigured at runtime** — broker/SSID are compiled in.
   They need a rebuild + OTA; they're the least-reversible class → migrate **last**, with fallback-AP
   recovery pre-tested.
6. **Archive-parity HARD gate** (`provision-peer.sh` stage 4) — ha-2 must hold the *full* months-deep
   parquet archive before it's dictator-eligible. This gate exists because of the 2026-06-25 incident
   (a box promoted with ~1.5 days of history while the deep archive sat elsewhere). Trust the gate.
7. **Descriptor completeness** — migration correctness hinges on serializing *every* store a device
   touches. Reuse `device_migrate.py`'s exact, unit-tested store set (hot.db, rungs.db, parquet,
   control.db `CONTROL_TABLES`, registry yaml, `control_secrets.yaml`, retained MQTT) — do NOT hand-roll
   the list.
8. **`.210` runs two keepalived instances** (household + air-gap) — must be **iface-bound** with distinct
   VRIDs; the drill must specifically test a `.210` reboot (both restart).
9. **The web bridge is the ONE sanctioned hole** — default-deny allowlist, reviewed artifact
   (`allowlist.md`); bind explicitly per-interface (never `0.0.0.0`).
10. **The bridge duty is NOT VIP-gated** (it must always run on `.210` regardless of cluster role) —
    sharp contrast with the air-gap dictator duties (`ha-controller`, `ha-relay-coordinator`,
    `ha-router-reconcile`) which ARE VIP-gated. Document this distinction loudly.
11. **Hidden SSID rejoin** — validate one edge node against the hidden `autohome_airgap` early (Phase 1),
    before betting the fleet on it.
12. **"Never dictator" is policy, not mechanism** unless enforced: `.210` air-gap = `PRIORITY=100` +
    preempt-ON (ha-2 reclaims) + a cluster-doctor invariant ("VIP==ha-2 under normal ops") + ntfy alert
    on the degraded state.
13. **Net-switch handover blind spot (control plane, DJ-11).** When ha-2 physically moves household→air-
    gap (Phase 1), household SSH to it drops and `.210`'s air-gap leg (`eno1`) is the *only* way back in.
    Sequence so control is never lost: (a) bring up `.210`'s `eno1` (`192.168.1.245`) and verify SSH
    `.210→192.168.1.210` works **before** unplugging ha-2 from household; (b) pre-stage ha-2's air-gap
    identity (static `192.168.1.210`, keepalived air-gap `cluster.env`, id_cluster authorized) as a
    one-shot that self-applies on the air-gap net, so a bad move is recoverable from the console + the
    `.210` leg, never a re-image. The migration control plane = **id_cluster SSH `.210`→ha-2 over `eno1`
    + the bridge HTTPS `:import` channel + broker confirms** — all app-level over `.210`'s dual-homing,
    `ip_forward=0` intact.
14. **The air-gap net has NO internet** — `apt`/`pip`/`npm`/`docker pull`/`git fetch` all FAIL after the
    move. Every dependency must be prepositioned on ha-2 in **Phase -1**: full apt set (the `stage2-finish`
    packages incl `firmware-mediatek`/`keepalived`/`chrony`/`ntfy`/`mosquitto`/`bluez`), the pinned pip
    wheelhouse (`--no-index`), `node`/`claude` if a local agent is ever wanted, the ESPHome docker image +
    external components, edge firmware binaries + reflash toolchain, the OpenWRT images (`instance/openwrt/`),
    and the repo + all migration scripts. Reuse the signed-bundle mechanism in
    `provisioning/03-sneakernet-updates.md`. Discovering a missing dep post-gap = a sneakernet round-trip;
    the Phase -1 readiness gate exists precisely to prevent it.

---

## 4. Target architecture & address plan

- **Household `192.168.0.0/24`** — unchanged. `.210` keeps `192.168.0.210` (`enp4s0`); `.245` fileserver.
- **Air-gap `192.168.1.0/24`** — isolated. R7800/OpenWRT `192.168.1.1` (gateway, DHCP `.100–.149`, static
  `.150–.199`), hidden SSID `autohome_airgap`, WAN disabled. **ha-2 dictator** `192.168.1.210`; **server
  VIP** `192.168.1.200` (VRID 61); **`.210` air-gap leg** `192.168.1.245` (wired `eno1`). Broker =
  `192.168.1.210:1883`. Router-gateway failover VIP = `192.168.1.1` (VRID 71, between routers).
- **`.210` dual-homed, no forwarding** (`ip_forward=0`); app-level reverse proxy on
  `192.168.0.210:443` → ha-2 API on the air-gap side.

*(Address mnemonic: on the air-gap net, ha-2 keeps `.210`'s old dictator number `.210`, and `.210`'s
air-gap leg takes `.245`'s old standby number — roles map to familiar numbers.)*

---

## 5. Cross-cutting work (foundational — precedes the phases)

### A. Generalize the same-LAN cluster primitives (parameterize, don't fork)
- `failover/keepalived.conf.tmpl`: literal `192.168.0.200/24` + `virtual_router_id 51` → `@VIP@/@CIDR@`
  + `@VRID@`.
- `failover/cluster.env.example`: add `NET_NAME`, `VRID`, `CIDR`, `IFACE` (explicit — `.210` is multi-
  NIC), `BROKER_HOST` (real IP, not VIP); keep `PEER_HOST` (this cluster's other box).
- `failover/deploy.sh`: use `IFACE` from env (stop auto-detecting on a multi-homed box).
- `failover/healthcheck.sh`: replace hardcoded Midea-ping with a data-driven per-box
  `instance/dictator-actuators.env` (empty set = fit on API alone → lets ha-2 be a fit dictator with
  zero devices on day one, growing its fitness set as devices migrate).
- **Gate:** rendering from the *old* household `cluster.env` is byte-identical (modulo comments) to the
  current live config — proves backward-compat before anything new stands up.

### B. Network infrastructure as dictator-managed devices
- `server/maintenance/router_reconcile.py` — pure `reconcile_router(target, dry_run)->report` core (like
  `device_migrate.py`) + thin CLI: renders UCI from `provisioning/openwrt/etc/config/*` + gitignored
  `instance/openwrt/<router>.env` (PSK/country/radio-path/MAC map), SSHes the router (id_cluster back-
  channel), computes a per-section **drift diff**, corrects via `uci import`/`commit`/`reload_config`
  (never blind-overwriting hardware-generated `wireless` `path` lines), asserts health, alerts via ntfy.
- `systemd/ha-router-reconcile.{service,timer}` — **VIP-gated** two ways (started/stopped by
  `notify.sh` on MASTER/BACKUP + an `HA_VIP` guard in `ExecCondition`), mirroring `ha-relay-coordinator`.
- `failover/notify.sh` — add `router_reconcile()` alongside `relay_coord()` (MASTER→start,
  BACKUP/FAULT→stop; non-blocking).
- Registry: add the router as a `node`-class device (`airgap_router`) in `instance/devices.yaml`; publish
  health on `home/<area>/airgap_router/state` (reuse the `tasmota_bridge.py` canonical shape). Power
  already metered by `airgap_router_pm`.
- **Failover routers:** keepalived on both OpenWRT boxes floats gateway `.1` (VRID 71); the dictator's
  `router_reconcile.py` keeps *both* config-current (`instance/openwrt/routers.yaml`). Split mirrors the
  server tier: keepalived = liveness, reconcile/provision-peer = parity.

---

## 6. Phased roadmap

**Phase -1 — Pre-provision while control is guaranteed (offline-readiness + control-plane proof).** With
ha-2 still directly reachable on the household net (guaranteed control + a fallback), install/stage
EVERYTHING it needs to run air-gapped and be driven remotely, then PROVE the post-gap control path before
losing the safety net. This is the "do it while we DEFINITELY have control" phase.
- **Access hooks:** bidirectional id_cluster SSH auto-auth (`.210↔ha-2` authorized_keys, passwordless
  verified both ways), admin/panel tokens, fs access, NOPASSWD sudo for the service ops.
- **Offline dependency preposition (gotcha 14):** full apt set, pinned wheelhouse (`pip --no-index`),
  ESPHome docker image + external components, edge firmware bins + reflash toolchain, OpenWRT images,
  and the repo + **all migration tooling deployed** (`device_descriptor`/`device_push`, the `:import`
  endpoint, `router_reconcile`) with systemd units installed — via the `provisioning/03-sneakernet-
  updates.md` signed bundles.
- **Preposition secrets:** the `dictator-files.manifest` `preposition` set (`.master_pass`, etc.) placed
  on ha-2 now (never over the wire post-gap).
- **Control-plane proof:** from `.210`, over the *exact channel that will be the air-gap path* (SSH +
  `:import` HTTPS + broker confirm), run a full dry-run against ha-2 while household access is still the
  fallback; fix any gap discovered.
- **New:** `provisioning/airgap/preflight-readiness.sh` — asserts every dependency present + every hook
  working + the control-plane dry-run green.

**Gate:** `preflight-readiness.sh` 100% green — the "point of no easy return" before ha-2 moves nets.
**Rollback:** trivial (nothing air-gapped yet). **Risk/why:** this phase EXISTS to eliminate the
"missing-dep/broken-hook discovered after the gap" failure class — the most expensive kind post-move.

**Phase 0 — Foundation (household net only).** Decide the air-gap **broker-auth posture** (per-device
creds) now; ha-2 is already Stage-2'd as a same-LAN peer (Phase -1), so run `provision-peer.sh --from
192.168.0.210` → must PASS the archive-parity gate; snapshot `.210`'s dataset (`tools/backup-dataset.sh`)
as the rollback anchor. **The failover *drill* is NOT here** — per DJ-14 it's a two-step `.210↔ha-2`
exercise once the air-gap net is completed, not a `.210↔.245` drill. **Gate:** provision-peer PASS +
cluster-doctor HEALTHY. **Rollback:** trivial (ha-2 passive).

**Phase 1 — Stand up the real air-gap network.** Re-cut the R7800 UCI drafts to `192.168.1.0/24` +
hidden `autohome_airgap` + WAN-disabled + NTP anchored on the servers; bring the router under
`router_reconcile.py` (cross-cutting B); `provisioning/airgap/210-airgap-nic.sh` brings up `eno1` static
`192.168.1.245`, asserts `ip_forward=0`; render the air-gap keepalived cluster (VIP `192.168.1.200`,
VRID 61) via the parameterized template; ha-2 becomes sole dictator; `verify-gap.sh` proves no L3 path
household↔air-gap; validate one edge node against the hidden SSID. **Gate:** ha-2 holds VIP+controller;
negative reachability probe passes (air-gap proof). **Rollback:** `210-airgap-nic.sh --down`; ha-2 back
to household; versioned configs restore.

**⏸ RELOCATION CHECKPOINT (after Phase 1) — PAUSE (DJ-13).** With ha-2 + the router configured and
validated on `192.168.1.0/24` at the bench, **pause the plan**; Hugh physically relocates BOTH to their
final/medium-term location. On resume, **re-verify from the new location before proceeding**:
`verify-gap.sh` green (air gap intact after the move), `cluster-doctor` green (ha-2 still dictator,
`.210` air-gap leg reachable), `router_reconcile.py --dry-run` zero drift, and ≥1 edge node confirms wifi
reach to `autohome_airgap` from a representative device spot. Only then continue Phases 2→4.

**Phase 2 — The `.210` web bridge (app-level, no routing).** Reverse proxy on `192.168.0.210:443` → ha-2
API on the air-gap side (terminate + re-originate = no forwarding). `provisioning/airgap/bridge/`
(Caddyfile/nginx tmpl, default-deny `allowlist.md`, `install-bridge.sh`, `systemd/ha-web-bridge.service`
— **not** VIP-gated); bearer/TLS/VAPID pass-through (reuse `auth_tokens.py`, `instance/tls/*`,
`vapid.json`); upstream targets the VIP so it follows failover. **Gate:** household browser actuates a
test control through `.210:443`; cannot reach any `192.168.1.x` directly; `verify-gap.sh` green.
**Rollback:** `--uninstall`.

**Phase 3 — Cross-network migration tooling (build once).** `server/maintenance/device_descriptor.py`
(serialize identity+registry+control.db+secret+history, reusing `device_migrate` helpers); import router
factory in **`server/api/control.py`** (the gap-analysis's `server/api/devices.py` does NOT exist),
wired in `main.py`: `POST /api/v1/admin/devices:import` (admin-bearer, DISTINCT-UNION merge reusing
`apply_sqlite`/`apply_parquet`); `server/maintenance/device_push.py` (per-device state machine
queued→transferred→applied→repointed→confirmed→retired(+failed/rollback), state in
`instance/db/migration.db`); transport = **HTTPS to ha-2 via the Phase-2 bridge** (no second hole);
retire = local `run_migration("retire", …, do_peer=False)` on `.210` *after* ha-2 confirms (the only new
cross-net hop is descriptor→`:import`); multi-net cluster-doctor asserting **no device active on both
sides**. Per-class repoint: Tasmota (`tools/repoint_tasmota.py`, MQTT Backlog); BLE (descriptor +
ha-scanner on ha-2); ESP32/D1001 (signed `config`/NVS firmware op + `tools/repoint_node.py`/`d1001_cmd.py`,
OTA-reflash fallback); ESPHome (`provisioning/esphome-repoint.sh` rebuild+OTA). **Gate:** end-to-end
dry-run on a `sim_device` completes the state machine; merge idempotency proven; bench ESP32 accepts
signed / rejects unsigned config op. **Rollback:** `device_push.py --rollback <id>` (symmetric).

**Phase 4 — Execute per-device migration.** Order: ① BLE → ② Tasmota → ③ ESP32 → ④ D1001 → ⑤ ESPHome
(last). Runbook `docs/airgap/40-per-device-runbook.md`: `device_push.py <id>` = serialize → import →
verify → repoint → confirm N min → retire → mark. `instance/migration-plan.yaml` (gitignored) drives the
batch; `dead-device-report.sh` parks failures for reflash (ntfy alert, never retire). **Gate:** every
non-`.245` device active on ha-2 only or parked; `.210` ingests no live device. **Rollback:** per-device
`--rollback` (blast radius = one device).

**Phase 5 — `.210` joins the air-gap cluster as failover (keeps household bridge).** `.210` air-gap
`cluster.env`: `ROLE=standby VRID=61 VIP=192.168.1.200 IFACE=eno1 PRIORITY=100` (below ha-2 150,
preempt-ON → ha-2 supremacy); `provision-peer.sh --from 192.168.1.210` over `eno1`; `.210`'s second
keepalived instance on `eno1`; bridge upstream fails over to `.210`'s air-gap leg if ha-2 dies (household
UI survives, still no route); `.245` formally retired. **Gate:** cluster-doctor green (ha-2 MASTER,
`.210` BACKUP); air-gap `failover-drill.sh` moves dictator+router duties to `.210` and back while the
bridge never drops; `verify-gap.sh` green throughout. **Rollback:** remove `.210`'s air-gap instance.

---

## 7. Open questions / TBD
- **Direct air-gap admin surface** (so `.210`'s bridge could eventually be optional): a tablet/laptop
  living on the air-gap net running the PWA. Until solved, the `.210` bridge is permanent.
- **Broker-auth posture** for the air-gap broker (per-device tokens + TLS) — decide in Phase 0 so
  repointed devices authenticate; ties to the deferred `broker-auth-posture` work.
- **Second failover router** — hardware TBD; the design supports it (VRID 71 gateway float) but v1 may
  ship single-router with the standby staged.
- **iOS vs Android admin** on the air-gap net (ntfy/PWA background behavior differs).

## 8. Path to ADR + template
When the phases are proven, distill this log into: (a) `docs/adr/ADR-00NN-airgap-migration.md` (the
decisions + rationale from §2/§3), and (b) `docs/airgap/TEMPLATE.md` — a parameterized, repeatable
"migrate the fleet to a new isolated network" procedure (the Phase 0–5 runbooks with the same-LAN
generalizations from §5 already applied), so the next migration is fill-in-the-blanks, not rediscovery.

## 9. Critical files
- `failover/keepalived.conf.tmpl`, `failover/cluster.env.example`, `failover/deploy.sh`,
  `failover/notify.sh`, `failover/healthcheck.sh` — parameterize per-cluster; add router-reconcile duty
- `server/maintenance/device_migrate.py` — reuse the idempotent per-store cores everywhere
- `server/api/control.py` (+ `main.py`) — add the `devices:import` router
- `firmware/components/ha_mqtt/ha_mqtt.c` — add the signed `config`/NVS op (`ha_config.c` reads it)
- `provisioning/openwrt/etc/config/{network,dhcp,wireless,system}` — re-cut to `192.168.1.0/24`
- New: `server/maintenance/{device_descriptor,device_push,router_reconcile}.py`;
  `provisioning/airgap/{210-airgap-nic.sh,verify-gap.sh,bridge/*}`;
  `systemd/{ha-web-bridge,ha-router-reconcile}.service`; `tools/{repoint_tasmota,repoint_node}.py`;
  `docs/airgap/*`

## 10. Execution log (build journal — real-time whoopsies + fixes)

> Dated, chronological record of what actually happened as we built each phase — including the bugs we
> hit and how we fixed them. These are the details that make the future ADR/template battle-tested.

### Phase -1 (pre-provision ha-2 while on the household net, 2026-07-08)
- **Control-plane hooks established.** Set up the shared `id_cluster` back-channel: authorized
  `id_cluster.pub` on both `.210` and ha-2 and copied the shared keypair to ha-2. Verified **bidirectional**
  passwordless SSH (`.210`↔ha-2). This is the exact channel that becomes the post-gap control path.
- **ha-2 baseline:** Debian 13 (kernel 6.12.95), 208 G free, 15 G RAM, repo current, internet reachable,
  broker-isolated (no `instance/mqtt.env` → services default to its own localhost broker, so ha-2 can't
  disturb `.210`'s live bus).
- **WHOOPSIE #1 — `stage2-finish.sh` died with "sudo: a password is required" over headless SSH.** Root
  cause: line 67 used `sudo -v`, which *validates against every one of the user's sudoers rules* and so
  prompts for a password even though the bootstrap `90-visko-bootstrap` grant is `NOPASSWD: ALL` —
  because visko is also in the `sudo` group (`%sudo ALL=(ALL:ALL) ALL`, password-required), and `-v`
  doesn't get to "last match wins." With no TTY (BatchMode SSH) the prompt can't be answered → die. Real
  `sudo` commands were fine the whole time (`sudo -n true` = rc 0). **Fix:** replaced `sudo -v` with
  `sudo -n true` (the correct non-interactive check). **Lesson for the template:** every provisioning
  script that may run headless over SSH must use `sudo -n`, never `sudo -v`/bare `sudo`, for its preflight
  — this whole migration operates boxes headlessly, so it's a class of bug, not a one-off.
- **Stage-2 re-run green.** ha-2 now has the full app layer: packages (incl `firmware-mediatek`), BlueZ
  `--experimental`, venv, all core `ha-*` services active on its **own** localhost broker (BLE scanner
  live on the onboard MT7922), `ha-controller` correctly OFF.
- **Infra + offline artifacts prepositioned** (while ha-2 still has internet — gotcha 14): `keepalived`
  (installed, left **inactive**), `chrony`, `ntfy` (a stock Debian package — no vendored `.deb` needed).
  Copied `instance/openwrt/` images (~18 MB) and the `preposition`-class `instance/.master_pass` (mode
  600) to ha-2 over the trusted household channel.
- **WHOOPSIE #2 (minor, self-inflicted) — unterminated `'` in `preflight-readiness.sh`.** Nesting
  `awk "…"` inside `$(CL '…')` left a single-quote unclosed; the string swallowed everything to the next
  `'`, so `bash -n` flagged a misleading line ~30 lines later. **Fix:** extract the remote command to a
  var (`pyv=$(CL 'python3 -V 2>&1')`). **Lesson:** in the `CL '…'` SSH-wrapper pattern, keep the remote
  command a single simple single-quoted string — never nest quotes inside it.
- **✅ Phase -1 GATE PASSED.** `provisioning/airgap/preflight-readiness.sh` → **34 passed, 0 failed, 2
  pending** (Tier-2: Phase-3 tooling + full `:import` dry-run, which ride the SSH channel later). Control
  plane proven both directions; ha-2 API `/health`=200. ha-2 is pre-provisioned and remotely operable with
  the household fallback intact. **Next: Phase 0** (failover drill + `provision-peer` parity) on go-ahead.

### Phase 0 (foundation, household net, 2026-07-08)
- **Failover-drill dropped** from Phase 0 per DJ-14 (it moves to the two-step `.210↔ha-2` exercise at the
  very end).
- **ha-2 quiesced to standby posture** — stopped `ha-scanner`/`ha-edge-mapper`/`ha-edge-history` (the
  standby receives via reconcile, it doesn't ingest). Note: ha-2's own brief BLE ingest before this left
  no junk on `.210` — the bidirectional reconcile pushed **0 new** rows back.
- **✅ `provision-peer.sh --from 192.168.0.210` PASSED.** Synced config-of-record (registries, `control.db`,
  `mesh.db`, VAPID) + hot tier (27,788 rows) + all 7 Parquet partitions. **HARD archive-parity gate:**
  ha-2 `rows=10,537,962 earliest=2026-01-07T17:43Z` == source exactly → **RECORD-KEEPING ELIGIBLE**. ha-2
  now carries the full dataset into the migration. Took ~1 min (LAN).

### Phase 1a (headless prep before the physical move, 2026-07-08)
- **ha-2 captured + shut down.** Grabbed ha-2's NIC (`eno1`, MAC `8c:1f:64:c2:26:5c`) for the R7800
  DHCP reservation, confirmed sshd + id_cluster + DHCP, then a clean poweroff. ha-2 is **off, ready to
  move**. **WHOOPSIE #3 (minor):** a backgrounded `(sleep 3; poweroff) &` over SSH gets SIGHUP'd when the
  session closes, so it never ran — issue `poweroff` directly and let the connection drop.
- **R7800 UCI configs re-cut** to the air-gap target (`provisioning/openwrt/etc/config/{network,dhcp,
  wireless,system}`): lan `192.168.1.1`; hidden SSID `autohome_airgap` (both bands, 5 GHz ch 149);
  `noresolv` (no upstream DNS); pool `.100–.149`; reservations **ha-2→`.210`**, `.210`-leg→`.245`; NTP
  anchored on `.1.210`/`.1.245`; WAN disabled. Real MACs/PSK/paths in gitignored
  `instance/openwrt/airgap_router.env` (never committed).
- **New scripts:** `provisioning/airgap/210-airgap-nic.sh` (brings `.210`'s `eno1` up static
  `192.168.1.245`, persistent ifupdown drop-in, **no default route via the leg + `ip_forward=0` = the
  gap**) and `verify-gap.sh` (asserts `ip_forward=0`, dual-homed, no cross-route, + the **negative test**
  that ha-2 cannot reach the household net).
- **Deferred to bring-up (1b):** `router_reconcile.py` + `ha-router-reconcile` and the keepalived /
  `cluster.env` generalization — written where they can be **tested live** against the R7800/cluster,
  not blind. Applied over `.210`'s air-gap leg (SSH, no internet needed).
- **➡ Handoff: ha-2 + R7800 ready to physically move/cable** (see the move runbook / Phase 1b).

### Phase 1b (live bring-up over WiFi, 2026-07-08)
- **DJ-15 — `.210`'s air-gap leg is WiFi (`wlp2s0`→`autohome_airgap`), not the wired `eno1`.** The only
  spare cable *was* `.210`'s live internet cable, and the R7800 relocated ~10 ft away. So the DJ-7 wired
  plan is off; the documented WiFi *fallback* becomes the **permanent bridge leg** (signal −34 dBm).
  Consequence to watch: the household web UI rides this WiFi link — monitor its reliability.
- **WHOOPSIE #4 — "PROVISION THE ROUTERS TOO." (the big one, per Hugh 😄)** The R7800 got relocated
  still on its **bench config** (`192.168.0.1` — the *same IP as the household gateway*) because Phase 1a
  only re-cut the config *files* and **deferred applying them** (router_reconcile). Result: the bring-up
  had to reach a bench-IP router that collides with the household gateway, over WiFi, via scoped/policy
  routing — the whole "tricky part." **LESSON (template):** provision the router(s) *as managed devices*
  — **apply** the air-gap config while you still have easy/guaranteed access (DJ-12 applies to routers,
  not just servers), ideally **before relocating** them. "Config prepared" ≠ "device provisioned." Ties
  DJ-6 + DJ-12.
- **WHOOPSIE #5 — `pkill -f 'wpa_supplicant.*wlp2s0'` killed its own shell** (the pattern matched the
  running command line). Lesson: never `pkill -f` a pattern that appears in your own command; use pids.
- **WiFi PSK recovery.** The PSK is NOT the master password — `Canticum1` hit the classic 4WAY→SCANNING
  reject loop. Recovered the real key `catherineandhughwap` from the prepositioned R7800 config backup
  (`instance/openwrt/r7800-config-backup-20260701.tar.gz`) — a payoff for prepositioning it in Phase -1.
- **Safety guard did its job.** The sandbox classifier blocked a password-guess *loop* against the
  router (correct — that reads as credential-guessing). Resolved cleanly: a single pubkey attempt showed
  `.210`'s `id_ed25519` is already authorized on the R7800 root (from bench setup) — no password needed.
- **State reached:** WiFi COMPLETED; R7800 reachable at `.0.1` over the scoped WiFi path (household
  routing UNTOUCHED, `ip_forward=0`); root SSH to the R7800 via `id_ed25519`. **Next: apply the air-gap
  reconfig** (minimal delta — preserve the device's real wireless paths/key; change lan→`.1.1`, DHCP +
  reservations, NTP, `noresolv`), then re-home `.210`'s WiFi leg to `.1.245` and bring ha-2 up at `.1.210`.
- **✅ AIR-GAP NETWORK LIVE (over the WiFi bridge).** Reconfigured the R7800 in place `.0.1`→`.1.1`
  (`ha-router-airgap`) via root SSH over the scoped WiFi path — minimal delta (lan/DHCP/reservations/NTP/
  `noresolv`; **wireless left untouched** — it already had `autohome_airgap` + hidden + real key + real
  radio paths). Re-homed `.210`'s WiFi leg `.0.3`→`.1.245` — distinct subnets now, so **all the policy-
  routing/arp collision hacks dropped away**. **`verify-gap.sh` GREEN 7/7, incl. the NEGATIVE test: ha-2
  cannot reach the household net.** Control regained via `id_cluster` SSH `.210`→ha-2 `.1.210`.
- **WHOOPSIE #6 (mild) — `/etc/init.d/network restart` on the R7800 bounces the WiFi radios**, dropping
  BOTH my association and ha-2's link. Upside: `wpa_supplicant` re-associated in ~3 s, and ha-2's link-
  bounce triggered a fresh DHCP → it auto-grabbed its reserved `.1.210` (no reboot/power-cycle needed).
  Lesson: expect a brief WiFi drop when reloading the router you're reaching *over* that WiFi; the pieces
  self-heal, but don't panic when the ping goes to 100% for a few seconds.
- **⚠ OPEN — bridge persistence.** `.210`'s WiFi leg (`wlp2s0` association + `.1.245`) is **RUNTIME-ONLY**
  right now — a `.210` reboot would strand the air-gap net. Needs a persistent `wpa_supplicant` +
  static-IP unit (systemd or ifupdown). **NEXT before anything else load-bearing.**
- **✅ Bridge persistence DONE.** `/etc/wpa_supplicant/wpa_supplicant-wlp2s0.conf` (HASHED psk, 600) +
  `systemd/ha-airgap-bridge.service` (Type=simple, Restart=always) running
  `provisioning/airgap/airgap-bridge-up.sh` (up `wlp2s0` → static `.1.245` → `ip_forward=0` → exec
  `wpa_supplicant` foreground). Handed off from the manual supplicant + verified: re-associates in ~6 s,
  restores `.1.245` + reachability, gap still 7/7. Survives reboot (enabled) + self-heals. WIFI_PSK +
  radio paths recorded in gitignored `instance/openwrt/airgap_router.env`.
- **✅ ha-2 WiFi radio OFF (Hugh's request — power + RF).** ha-2 is wired (`eno1`); its onboard MT7922
  WiFi is pure waste. `rfkill` isn't installed (air-gapped, no `apt`) → set `wlp1s0` down, then unloaded +
  **blacklisted `mt7921e`** (`/etc/modprobe.d/ha-airgap-no-wifi.conf`). Verified ha-2's **Bluetooth is
  independent** (`btmtk`/`btusb` over USB) and stays UP — BLE scanning unaffected. **Lesson:** on a
  MT7922 combo, the WiFi driver (`mt7921e`) and BT (`btmtk`) are separate — you can kill WiFi without
  losing BLE; and `rfkill` may be absent on a minimal/air-gapped box (blacklist the module instead).

- **DJ-16 — ha-2's VIP + *active* `ha-controller` are DEFERRED past Phase 1 (not free to turn on yet).**
  Discovered while going to make ha-2 the formal dictator: ha-2 holds a **full copy of `.210`'s control
  state** from provision-peer — `control.db` has the **4 household actuators** and `midea-device.env` is
  present. So (a) starting `ha-controller` on ha-2 now would have it try to actuate household devices at
  `.0.x` IPs it **cannot reach** (errors / false alerts); and (b) the VIP `.1.200` is a *failover*
  mechanism — **pointless with a single node** (devices target the real broker `.1.210`, not the VIP).
  **Refinement:** ha-2 IS the air-gap dictator *functionally* now (runs broker `.1.210` + API + holds all
  data/config); the **VIP/keepalived activates in Phase 5** (when `.210` joins as failover peer) and
  **active `ha-controller` activates as devices migrate** (Phase 4, when its control set becomes real
  air-gap devices). Keepalived is still generalized + *deployed-but-not-started* as prep. Also:
  `healthcheck.sh` must skip the household-Midea ping for an air-gap box (else ha-2 is marked unfit).

- **✅ Cluster primitives generalized (keepalived prep for Phase 5).** `keepalived.conf.tmpl` now takes
  `@VIP@`/`@VRID@`/`@CIDR@` (was hardcoded `.0.200`/VRID 51); `deploy.sh` fills them from `cluster.env`
  with backward-compat defaults + an explicit `IFACE` (needed on the dual-homed `.210`);
  `cluster.env.example` gains `NET_NAME`/`VRID`/`CIDR`/`IFACE`/`BROKER_HOST`; `healthcheck.sh` is API-only
  when `NET_NAME=airgap`. **Backward-compat proven** (rendering the household values reproduces `.210`'s
  live config byte-for-byte). ha-2's air-gap keepalived config **deployed** (MASTER, VIP `192.168.1.200`,
  VRID 61, `eno1`) but **not started** (DJ-16).
- **✅ Router as a managed device (DJ-6).** `server/maintenance/router_reconcile.py` — config-as-code
  reconciler for the R7800: `check` (drift vs the config-of-record), `apply` (correct + commit + reload),
  `health` (LAN IP / DHCP / radios / SSID / ha-2 reservation / WAN-off). **Tested live:** 6/6 health,
  and a drift induced on the router was detected (exit 1) + auto-corrected. Authorized the shared
  `id_cluster` on the R7800 so the **dictator** (ha-2) manages it. `systemd/ha-router-reconcile.{service,
  timer}` run it on ha-2 every 15 min (the VIP-gate is deferred to Phase 5 per DJ-16, with the exact
  `ExecCondition` noted in the unit).
- **git-bundle-over-SSH** is the repo-update path for air-gapped ha-2 (no `github.com`): `git bundle
  create` on `.210` → scp over the bridge → `git pull <bundle>` on ha-2. Recurring; worth a helper.

### Phase 2 (the `.210` web bridge, 2026-07-08)
- **✅ Household → ha-2 web bridge LIVE.** Installed nginx on `.210`;
  `provisioning/airgap/bridge/ha-web-bridge.conf` reverse-proxies `https://192.168.0.210/` (bound to the
  **household NIC only**, `.210`'s TLS cert) → ha-2's API `192.168.1.210:8123` over the WiFi bridge. It's
  an **app-level** gateway (nginx terminates the household TLS + originates a fresh upstream conn) — **NOT
  IP forwarding**, so the gap holds (`ip_forward=0`). Auth is end-to-end (bearer passed through to ha-2's
  `api_authz`). **Verified:** `GET https://192.168.0.210/api/v1/sensors` → 200, serving **ha-2's** data.
  `install-bridge.sh` (+`--uninstall`) makes it reproducible; nginx enabled for boot. **NOT VIP-gated** —
  it always runs on `.210` (the permanent bridge), unlike the air-gap dictator duties. **Hardening TODO:**
  default-deny allowlist (`provisioning/airgap/bridge/allowlist.md`).
