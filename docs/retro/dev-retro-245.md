# Dev retrospective — the dictator ↔ failover build (245 / ops perspective)

*Author: `ops` (the `.245` warm-standby / desktop-side agent). Independent half of a two-perspective
retro (pair: `dev-retro-210.md`); to be synthesized into `dev-retro-synthesis.md`. Written 2026-06-24.*

## 1. History — what we actually did

The arc from "one box" to "a failover pair with an out-of-band control plane," in order:

1. **Made the repo public + scrubbed history.** Before publishing I found PII Hugh believed wasn't there —
   the master passphrase, real MACs, geo-coordinates — in the *tree and the git history*. Scrubbed via
   `git filter-repo`, placeholdered, force-pushed; scrub-only (no rotation, by decision).
2. **Brought up the G11 ("ha-dev"/210)** from a reusable Debian preseed: hit the RTL8125 slow-relink netcfg
   gotcha, hardened the template (link-wait, early-up all NICs, wifi fallback), fully-unattended partitioning.
3. **Promoted 210 to sole dictator, demoted `.245` to warm standby** via a *gated* cutover run between two
   Claude sessions, with Hugh holding the go-points (G1/G2/G3) and an "exactly one controller" invariant
   checked at every step. Zero split-brain; the Midea never moved during the swap.
4. **Built keepalived/VRRP failover** with the core rule **PRIMARY SUPREMACY**: the standby is only ever a
   *temporary* stand-in — it auto-demotes the instant the primary returns healthy, and can never be
   permanently promoted without Hugh. Runtime is **LLM-free and GitHub-free** (keepalived + systemd + bash).
   Full fail-over/fail-back cycle tested live, including real Midea actuation from `.245`.
5. **Added an out-of-band cluster bus** — MQTT `ha/cluster/#` heartbeats (bridged between brokers) + HTTP
   `/cluster/*` RPC + SSH — so the boxes sense each other without GitHub.
6. **Evolved agent-to-agent coordination** through three generations: a shared runbook (→ merge chase) →
   per-side status files (killed the chase) → a durable **RPC task ledger** (`ha/agents/#`, dependency-aware)
   → an **interrupt-driven wake layer** (free watcher → headless runner, zero idle cost).
7. **Hardened the failover** post-go-live: startup-transient suppression, an MQTT cross-check for the
   standby's yield trigger, consistent DB snapshots, the cluster-bus bridge.

## 2. What went well — emphasize these in future dev

- **A deterministic invariant beat clever arbitration.** PRIMARY SUPREMACY means split-brain has a *fixed*
  tiebreaker (standby always yields to a healthy primary) — no election cleverness, no LLM in the loop.
  Future authority/coordination decisions should reduce to one such rule wherever possible.
- **Runtime independence from us.** The failover survives with no LLM and no GitHub. This is the single most
  important property: the agents *build* the system, they are never *in* its critical path. Keep it that way.
- **Gated handoffs with explicit go-points + an invariant check at each step.** "Never blind-deploy on the
  live dictator." Every state change was paired with a proof (direct Midea read, `vip_held`, `is-active`).
- **Idempotent, no-LLM finisher scripts** (`stage2-finish.sh`, `failover/deploy.sh`) — reproducible bring-up
  a human can run. The bring-up knowledge lives in scripts, not in a chat transcript.
- **Verify-after-push discipline.** After a silent `git push | tail` masked a non-fast-forward reject and
  caused divergence, we made `HEAD == origin/main` a mandatory post-push check. It's caught drift since.
- **Dogfooding.** We used the coordination ledger to coordinate building the coordination ledger; dev acked
  the protocol *through* the protocol. The system proved itself by being used.
- **Security caution paid off.** Surfacing the PII before publish, and refusing to put secrets in
  transcripts/logs, avoided a real leak.

## 3. What could have gone more smoothly

- **The merge chase.** Two agents editing one shared runbook collided immediately (Hugh predicted it). We
  lost cycles before switching to per-side files. **Lesson: partition writable artifacts by owner from the
  very first handoff — never two writers on one file.**
- **The silent push failure** that diverged the bus. **Lesson: a quiet pipe can hide a rejected push; always
  assert sync.**
- **Hugh was the rendezvous for too long.** Every cross-agent handoff needed a human relay. The RPC ledger +
  wake layer fix this, but arrived late — coordination infrastructure should be built *early*, before the
  work that needs it.
- **Over-conservative dependency modeling.** I gated ADR-0015 Phase 0 on "finalize" when the ADR says Phase 0
  is the *prerequisite that runs first*; dev correctly ignored my edge. **Lesson: derive deps from the design's
  stated order, not caution.**
- **Box-capability assumptions.** I assumed I could host a wake runner on the desktop, then found it has no
  `claude` CLI. **Lesson: probe each box's capabilities (CLI, node, sqlite3, broker/VIP reachability) before
  assigning it a role.**
- **A correctness gap found only post-go-live** (the keepalived boot BACKUP→MASTER controller blip). **Lesson:
  model a daemon's *startup state sequence*, not just its steady state, before going live.**

## 4. What to build next — make bring-up of new things smoother

### New failover boxes
- **A one-command `join-cluster`**: given a fresh box + role, do keys + `cluster.env` + `deploy.sh` +
  preflight + the supervised go-live, idempotently. Most pieces exist; unify them behind one entry point.
- **A capability preflight** that refuses a role until it has verified: keepalived present, broker + VIP
  reachable *from this box's segment*, `sqlite3` present, cluster SSH bidirectional, time synced.
- **A `cluster-doctor`** that asserts the invariants on demand and after every failover: exactly one
  controller, VIP held by exactly one node, heartbeats fresh, sync timer healthy, `.master_pass` present.

### Setting up a brand-new network (the air-gapped end-state)
- **Make the VIP routable across *every* segment, and prove it.** The wifi-can't-reach-VIP finding shows
  segment-aware reachability must be *designed*, not assumed. The OpenWRT router should own DHCP reservations,
  inter-segment routing for the VIP, and broker/OTA reachability.
- **A reachability matrix as a first-class, tested artifact**: rows = segments (wired/wifi/iot-vlan),
  columns = {VIP, broker, OTA host, NTP, dictator API} — green/red, re-run on any network change.
- **VIP-first addressing everywhere** ("address the role, not the box"), validated per-segment so a dictator
  swap is transparent to *all* clients, not just wired ones.

### New edge nodes
- **Lead with the FIRMWARE-GUIDE one-shot path** (dev built this) + a node-enrollment flow: secrets-default →
  enroll → coverage assignment (ADR-0015), no server code change to add a node.
- **Bake in the BLE/Wi-Fi coex time-share pattern** (dev's finding: one 2.4GHz radio, duty-cycle the BLE scan
  so wifi stays up) as a documented default, so Ethernet is an upgrade, not a dependency.
- **A node-registration RPC**: a new node announces itself; the dictator resolves coverage from the registry.

### New sensors / actuators
- **Trait-based induction (ADR-0002)** so a new device shape needs no new tier and no admin code — add by
  data (registry), not code.
- **Standardize the things we already proved**: calibration offsets (display-only), fallback-sensor chains,
  signed per-device directives (HMAC). A new actuator should inherit these by default.
- **An enrollment checklist + a smoke test** (does it decode? does a command round-trip with an ack?) so
  "added" always means "verified," never just "configured."

### Coordination / dev / handoff itself
- **Keep extending the RPC ledger + wake layer**: ops polls, dev is woken, work serializes on deps. This is
  the backbone — make it the *first* thing stood up on any new multi-agent effort, not the last.
- **A reusable "handoff gate" pattern**: explicit go-points + per-side status + an invariant check +
  never-blind-deploy, as a checklist any cutover instantiates.
- **Capability-aware agent placement** as policy (which box can host a runner, who polls vs. who's woken).
- **One "bring-up playbook"** that composes the preflight + idempotent finishers + invariant checks for each
  bring-up class above — so a new box / network / node / device is a *script with checkpoints*, not a memory.

---
*Synthesis note for the pair: I expect the 210 view to be stronger on the device/firmware + on-box-dictator
side (BLE coex, OTA, enrollment, the live-control continuity proofs). Where we overlap — gated handoffs,
runtime independence, the coord ledger — that convergence is itself a signal those are the load-bearing ideas.*
