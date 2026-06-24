# Dev retrospective — SYNTHESIS (dictator ↔ failover build)

*Synthesis of two independent write-ups: `dev-retro-245.md` (ops / `.245` failover side) and
`dev-retro-210.md` (dev / 210 dictator side). Authored by `ops`; **reviewed + extended by `dev` (210) — see
§F**. 2026-06-24. This is our joint output.*

The two perspectives were written without seeing each other and **converged hard**. Where they agree, treat
it as a proven principle. Where they're complementary, the union is the lesson. Where they conflict, resolved below.

## A. Convergent principles — both sides found these independently (highest confidence)

1. **Gate irreversible changes, and verify state — not intent — on the bus.** Both of us bound every
   home-affecting step to an explicit GO gate *and* a machine-checked proof. dev held G2 until the bus showed
   `.245 STOPPED`; ops checked `vip_held`/`is-active`/direct-Midea-read at each step. The invariant ("never two
   `ha-controller`s") was written down first and asserted continuously. **This is the #1 reusable habit.**
2. **Per-writer ownership of files beats a shared file, always.** The merge chase ended the instant we split
   into `210-status.md` / `245-status.md` / Hugh-owned gates. Never two writers on one artifact.
3. **Build the coordination substrate EARLY.** Both independently named "rendezvous was manual too long" as the
   biggest drag — Hugh relayed "go check" all session. The RPC ledger + wake layer fixed it but arrived late.
   On any new multi-agent effort, the bus is the *first* thing stood up, not the last.
4. **Verify VIP reachability per-segment before treating it as a convention.** ADR-0015 Phase 0's "address the
   VIP" was too clean — a Wi-Fi segment can't ARP the VIP secondary. Both flagged it. Reachability is
   *designed and tested per segment*, not assumed.
5. **Validate remote-capabilities on the bench while still recoverable.** OTA validated with USB un-brick
   available surfaced the real costs (enrollment, host-pin) before deploy. "Verified," never just "configured."
6. **Hierarchy + autonomy on a *written* whitelist** (Hugh gates governance/irreversible; agents act on
   `POLICY.md`). Move fast without routing around the gate.
7. **Write gotchas down while fresh** (`FIRMWARE-GUIDE.md`) and **capture bring-up in idempotent no-LLM scripts**
   (`stage2-finish.sh`, `deploy.sh`) — knowledge lives in scripts/docs, never only in a transcript.

## B. Complementary lessons — each side's unique, durable contribution

**From the failover/ops side (245):**
- A **deterministic invariant beats clever arbitration**: PRIMARY SUPREMACY (standby always yields to a healthy
  primary) makes split-brain resolution fixed and LLM-free.
- **Runtime independence from the agents** — failover runs on keepalived+systemd+bash, no LLM, no GitHub. The
  agents build the system; they are never in its critical path.
- **Verify-after-push** (`HEAD == origin/main`) after a silent push reject diverged the bus.
- **Capability-aware agent placement** — discovered the desktop has no `claude` CLI; a box's role must follow
  its actual capabilities (CLI, node, sqlite3, broker/VIP reach).
- **Security caution** — surfaced PII before the public scrub; secrets never touch transcripts/logs.

**From the dictator/dev side (210):**
- **Diagnose by single-variable isolation, empirically** — the box-vs-VIP A/B and BLE-auto-pause-during-OTA
  settled multi-cause confusion in one flash. Decisive tests > speculation.
- **Fork-and-swap for firmware** — S3 reused C6's proven modules, changed only the network layer. Legible diffs.
- **The BLE/Wi-Fi coexistence pattern** — one 2.4GHz radio; duty-cycle the BLE scan so Wi-Fi survives →
  Ethernet is an upgrade, not a dependency.
- **Enrollment is a prerequisite, not a discovery** — `HA_CMD_SECRET ""` silently rejecting commands surprised
  the OTA mid-flight. "A node that can be commanded" must list enrollment as a named precondition.
- **The data plane doesn't fail over yet** — control fails over; sensor *history* + `mesh_links` + `ha-api` do
  not (ADR-0015 §transparency). The most important open architectural gap.

## C. Resolved tensions

- **Should the coord board survive a failover?** dev suggested bridging `ha/agents/#` or persisting off the VIP
  broker. **Decision (Hugh): ephemeral** — re-seed after a swap; coordination state is transient. Revisit only
  if it bites. *But* dev's adjacent idea — **gated task types** (encode "needs Hugh's GO" in the dependency
  graph) — is adopted as a backlog item; it's the natural next evolution of the ledger.
- **Should ops also be on a wake-watcher (symmetry)?** Desired, but the desktop has no `claude` CLI and `.245`
  has none either. **Resolution: capability-aware** — current model is *ops polls, dev is woken*; full symmetry
  needs a CLI on an ops-side box (install on `.245`, or host an ops runner on 210). Not blocked, just scoped.

## D. Unified forward build plan (the best of both lists)

- **New failover box:** one idempotent `join-cluster` entry (cluster.env + keepalived + heartbeat +
  sync-standby) **+ a capability preflight** (keepalived/sqlite3/SSH/VIP-reach per segment) **+ a `cluster-doctor`**
  invariant checker (exactly-one-controller, one VIP holder, fresh heartbeats) run on demand and post-failover.
  **+ close the data-plane gaps** (ADR-0015 §transparency): history reconciliation across a swap, `mesh_links`/
  assignment replication, `ha-api` floating on the VIP.
- **Brand-new network:** a `network-init` runbook/script (sibling of `stage2-finish.sh`) that stands up the
  broker + first dictator + registry, and **gates on a VIP-per-segment reachability matrix** (segments ×
  {VIP, broker, OTA, NTP, dictator-API}, re-run on any change). The OpenWRT router owns inter-segment routing.
- **New edge node:** promote `FIRMWARE-GUIDE.md` §7 into a tool — `node-bringup <board> <id>`: enroll → build →
  flash → verify-relay → bench-OTA, with coex/duty-cycle + enrollment + OTA-validation as **automatic gates**.
- **New sensor / actuator:** a trait-based (ADR-0002) `add-device` flow — registry append (+ for actuators:
  enroll + `control.yaml` + broker ACL) + ADR-0015 relay-assignment + a smoke test (decodes? command round-trips
  with ack?). Inherit calibration/fallback/HMAC by default.
- **Coordination itself:** keep the RPC-ledger + wake layer as the backbone; add **gated task types**; make agent
  placement **capability-aware**; and **keep running this write-independently → synthesize loop** — the cheapest
  quality multiplier we have.

## E. Top 3 to do first (joint recommendation)
1. **`cluster-doctor` + capability preflight** — cheap, high-leverage safety for the next box/network, and it
   encodes the invariant-first habit both sides ranked #1.
2. **Close the data-plane failover gaps** (ADR-0015 §transparency) — the biggest known architectural hole;
   today a swap silently diverges history and drops the dashboard.
3. **`network-init` with a VIP-per-segment gate** — the air-gapped OpenWRT router is the moment to get
   segment-aware reachability right *by design*, fixing the wifi-VIP gap at the root.

## F. Dev (210) review — endorsement + additions
*Reviewed by `dev` (210), 2026-06-24. The synthesis is accurate and I endorse it: the §A convergence matches
what I found independently, and §B attributes the complementary lessons correctly. Four sharpenings from the
on-box side:*

1. **Isolate EARLY — the costly half of "diagnose by isolation."** §B credits the box-vs-VIP A/B and the
   BLE-pause-during-OTA as decisive. The missing lesson is *when*: I ran them only after ~6 reflashes and
   several long watches chasing the flaky Wi-Fi, conflating coex vs signal vs VIP-reachability. **The
   single-variable test is cheapest as the FIRST move, not the tie-breaker after a thrash.** Encode it: a node
   that won't hold a connection → A/B (box vs VIP) + check the coex duty-cycle *before* the second reflash.
2. **Wake-layer timing: don't wake `dev` mid-interactive-turn.** When Hugh is live with interactive-`dev`
   *and* a wake fires the headless `dev` runner, two `dev`s run at once. Cooldown + claim-tiebreak + rebase
   *guard* it, but the clean rule is: wake `dev` when its beacon says it's idle/PAUSED, not mid-turn. (A
   timing dimension on §C's "capability-aware placement.")
3. **Node operability is a bring-up gate, not an afterthought.** A deployed node you can't read is one you
   can't trust. Add to "new edge node": **error-only status LEDs** (off when healthy; slow, human-readable
   patterns per fault class) + a **published error-code table**, so a human diagnoses a remote node at a glance
   without a serial cable. (Queued: `led-error-codes`.)
4. **Correction (§C):** "ops polls, dev is woken" — `ops` isn't polling; it runs when Hugh engages the desktop
   session (human-triggered). The real asymmetry is **dev = wake-triggered, ops = human-triggered**, until an
   ops-side box gets a `claude` CLI + watcher. Conclusion unchanged; just the mechanism.

**Both perspectives are now written, reviewed, and reconciled — this synthesis stands as our joint output.**

## Thesis (merged)
**Invest in the substrate early — the bus, the runbooks, the bench gates, the gotcha-docs, the invariant
checks — because every new box, node, and device pays the same tax until you do; and gate the irreversible,
verifying state on the bus rather than trusting intent.**
