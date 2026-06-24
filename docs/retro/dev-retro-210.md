# Dev retrospective — 210 (dev) perspective

*Author: the on-device `dev` agent (210). Independent write-up per Hugh's request; the `ops` (245)
companion is `dev-retro-245.md`; we synthesize in `dev-retro-synthesis.md`.*

## The arc (what we actually did)

1. **Resumed** ha-dev (G11) Stage-2 provisioning → confirmed it was the live-but-not-yet-promoted dictator.
2. **Dictator handoff:** a *gated* cutover (G1 stop-245-controller → G2 start-210-controller → G3 demote-245),
   coordinated through `docs/cutover/` per-side status files, with Hugh holding the GO gates. `.245` → warm
   standby. The hard invariant — *never two `ha-controller`s* — held; the Midea never lost a master and never
   got two. `.245` then built keepalived/VRRP failover (VIP `.200`) and it **passed a full test** (real
   actuation + auto-demote).
3. **ADR-0015:** designed edge relay-coverage assignment, and — pushed by Hugh's questions — extended it to
   VIP transparency and failover *state continuity* (the data plane, not just control).
4. **Edge node:** brought the ESP32-S3-POE-ETH from bare board to a working relay — W5500 Ethernet, Wi-Fi
   fallback, then hardened it (coex duty-cycle, unbounded reconnect+watchdog) and **validated secured OTA
   end-to-end** on the bench.
5. **Coordination:** evolved from git-as-bus → a structured RPC task ledger (`coord.py`) → an interrupt-driven
   wake-watcher, so the two agents serialize work without Hugh relaying every "go look."

## What went well (emphasize this in future dev)

- **Gated, invariant-first handoffs.** Writing the split-brain invariant down and binding each step to a GO
  gate made an irreversible, home-affecting cutover *safe*. I held G2 until the **bus showed** `.245 STOPPED`
  — not a verbal "go." **Verify state on the bus, don't trust intent.** This is the single most reusable habit.
- **Per-writer files = conflict-free async coordination.** Splitting the status into `210-status.md` /
  `245-status.md` / Hugh-owned `GATES.md` ended the merge-chase instantly. Per-writer ownership beats a shared
  file every time.
- **Fork-and-swap for firmware.** The S3 reused the C6's proven modules verbatim and changed *one* thing
  (the network layer). Low-risk, fast, and the diffs were legible.
- **Validate on the bench while recoverable.** Doing the OTA *now*, with USB un-brick available, surfaced the
  real "extra work" (enrollment, host-pin) before the node ever went somewhere inconvenient. Bench-gate every
  remote-capability before deploy.
- **Diagnose by isolation, empirically.** The A/B (broker `.210` vs VIP `.200`) settled "Wi-Fi-flaky vs
  VIP-unreachable" in one flash. The OTA's flawless download (BLE auto-paused) *proved* the radio-coexistence
  theory. Decisive single-variable tests > speculation.
- **Document gotchas as you hit them.** `FIRMWARE-GUIDE.md` exists because we wrote down the
  ISR-before-W5500 / coex-duty-cycle / enroll-or-be-rejected traps *while they were fresh*.
- **Hierarchy + autonomy, not either/or.** Hugh-as-gatekeeper for irreversible/governance calls, agents
  autonomous on a *written* whitelist (`POLICY.md`). That balance let us move fast without routing around the gate.

## What could have gone more smoothly

- **The flaky-Wi-Fi rabbit hole.** I burned ~6 reflashes and several long broker-watches conflating three
  causes (coex contention, raw signal, `.200` reachability). The A/B that disentangled them should have come
  **early**, not after the thrash. *Rule: when a node won't stay connected, run box-vs-VIP A/B and check the
  coex/duty-cycle FIRST, before iterating.*
- **The VIP-transparency assumption was too clean.** ADR-0015 Phase 0 said "address the VIP" as if it were
  universal; a Wi-Fi segment couldn't ARP the VIP secondary IP even on-subnet. *Verify VIP reachability
  per-segment before adopting it as a convention.*
- **Rendezvous was manual for too long.** The git bus is an async dead-drop; Hugh had to relay "go check"
  repeatedly. The wake-watcher fixes it but arrived late — earlier investment in the RPC channel would have
  cut Hugh's relay burden across the whole session.
- **Self-inflicted git churn.** Rapid commits made me a moving HEAD for `ops`; an executable-bit flip caused a
  pull-abort. Fixed (`core.fileMode false`, batching, per-side files) — but it was friction we could have
  pre-empted with a coordination convention up front.
- **Enrollment surprised the OTA.** `HA_CMD_SECRET ""` silently rejecting *all* commands, and `enroll_node.py`
  regenerating `secrets.h` (adding broker creds), were mid-flight surprises. *Enrollment should be a named
  prerequisite of "a node that can be commanded," not a discovery.*

## What to build next (to make new-X bring-up smooth)

- **New failover box:** a single idempotent `failover/deploy.sh` run (cluster.env + keepalived + heartbeat +
  sync-standby), plus closing the **state-continuity gaps** (ADR-0015 §transparency): time-series
  reconciliation across a swap, `mesh_links`/assignment replication, and `ha-api` floating on the VIP. Today
  the *control* plane fails over; the *data* plane and history don't.
- **Brand-new network from scratch:** a "network-init" runbook/script that picks + **verifies** the VIP per
  segment, stands up the broker + first dictator, and seeds the registry — modeled on `stage2-finish.sh` but
  for the cluster. The VIP-per-segment check should be a gate, not a footnote.
- **New edge node:** turn `FIRMWARE-GUIDE.md` §7 into a *tool* — `node-bringup <board> <id>` that enrolls →
  builds → flashes → verifies relay → bench-OTA, with the coex/duty-cycle and OTA-validation as automatic gates.
- **New sensors/actuators:** an "add-device" flow — append to `instance/devices.yaml` (sneakernet today),
  and for actuators also enroll + write `control.yaml` + the broker ACL — plus ADR-0015 relay-assignment so a
  new sensor's best relay is chosen, not every relay flooding it.
- **Coordination itself:** put `ops` on a wake-watcher too (symmetry), make the ledger **survive a failover**
  (bridge `ha/agents/#` or persist off the VIP broker), add *gated* task types (a task that needs Hugh's GO
  encoded in the graph), and keep exercising this review→synthesize loop — it's the cheapest quality multiplier
  we have.

## One-line thesis
**Invest in the substrate early — the bus, the runbooks, the bench-validation gates, the gotcha-docs — because
every new box, node, and device pays the same tax until you do.**
