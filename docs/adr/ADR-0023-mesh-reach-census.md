# ADR-0023 — Mesh reach census: decouple observation from actuation

Status: **Accepted** (Hugh, 2026-07-02 — direction) — core decision locked; the tuning constants
(census interval, RSSI EWMA time-constant, reach retention) are knobs to settle during implementation.
Extends [ADR-0015 edge-relay-coverage-assignment](ADR-0015-edge-relay-coverage-assignment.md), builds on
[ADR-0010 mesh-topology-routing](ADR-0010-mesh-topology-routing.md), and reuses the shadow-tuner pattern
from [ADR-0016 failover-history-reconciliation](ADR-0016-failover-history-reconciliation.md). Module-first
per [[feedback-modularize-new-architecture]] (ADR-0020).

## Context

Phase B (ADR-0015) assigns each edge node a **relay allowlist**: the coordinator
(`server/mesh/coordinator.py`) reads a reach graph (`mesh_links`) and, via `best_relay()` (Dijkstra with an
RSSI-weighted `link_cost`, ~1.0 cost / 10 dBm), tells each node which meters it is the preferred source for
so it stops relaying the rest — saving edge radio/energy. Changes are debounced by a **900 s dwell**
(`reconcile(dwell_s=DEFAULT_DWELL_S)`) and links carry a decaying `adv_score` reception rate. So among the
links it can *see*, the mesh already rebalances and is hysteresis-debounced.

The flaw is **structural: observation is coupled to actuation.** `mesh_links` is populated *only from adverts
a node actually relays* (`record_link()` fires when the mapper ingests a relayed advert). But the relay
allowlist — the very mechanism that saves energy — makes a node **stop relaying** everything outside its
assignment, which also blinds the coordinator to everything outside that assignment. Consequences:

1. **No organic discovery.** A node can only be found a *better* source for a meter it is *already* relaying.
   Move it closer to an unassigned meter and it never reports that RSSI, so coverage never shifts. Rebalancing
   is confined to already-exercised links.
2. **Fossils.** A node gated to `relay-none` (or physically relocated) reports nothing, so its graph freezes
   at the old reality and its assignment epoch never advances — a chicken-and-egg deadlock: the gate
   suppresses exactly the observations needed to lift the gate.
3. RSSI is stored as the **latest value** (`COALESCE`), not smoothed; only the rate score decays. The 900 s
   dwell papers over the resulting noise rather than smoothing the signal.

**Motivating incident (2026-07-02):** `c6-bench` was relocated from the bench to the H bedroom as a permanent
dual-role node (SGP-40 gas + BLE relay). Its gas lane came up fine, but its BLE relay is stuck at the
**epoch-3 `relay-none` fossil** written 2026-06-24 when it fed `.245` — it publishes no relayed adverts, so the
coordinator cannot re-derive its new H-bedroom coverage. The only recovery today is a **manual epoch bump**.
That manual nudge does not scale, and the fossil class recurs on every relocation or gating event.

## Decision

Add a lightweight **reach census** that decouples *what a node hears* (cheap, always reported) from *what a
node relays* (bulky, still filtered). The relay-filter and its energy savings stay exactly as they are.

1. **Every edge node periodically emits a reach census**, independent of its relay allowlist, on a dedicated
   topic `home/edge/<node>/reach`: a compact list `[{mac, rssi_ewma, count, last_heard}, …]` of every endpoint
   it heard in the window. This is **metadata, not the advert firehose** — a node still filters the actual
   relayed adverts, so radio/energy savings are preserved; only a small periodic summary is added.
2. **Smooth at the source.** The node maintains an **EWMA of RSSI per heard MAC** and reports the smoothed
   value, so errant single-sample readings never reach the coordinator.
3. **Coordinator ingests the census through the existing passive-sighting path.** `record_link(..., ok=None)`
   is already documented as "a passive sighting — refreshes rssi/last_ts"; the census lights up this
   intended-but-unused hook. `mesh_links` now reflects the whole neighborhood → `best_relay()` sees reach a
   node isn't currently relaying → organic rebalancing, and **no node is ever invisible** (a `relay-none` node
   still censuses).
4. **Keep the 900 s dwell** at the assignment layer. Census EWMA + `adv_score` decay + dwell together provide
   the "historical logging + hysteresis" that smooths errant data without churning epochs.
5. **Optional reach-history log + shadow-tuner** (mirroring ADR-0016): log the reach series so the
   census interval and EWMA constants can be tuned on real data before they drive assignments.

**Module boundaries (module-first):**
- **Census emitter** — a shared edge-firmware capability (new `ha_reach` component, or folded into
  `ha_ble_scan`/`ha_relay`): passive scan → per-MAC RSSI EWMA → periodic publish. On all relay-capable nodes.
- **Coordinator ingest** — a server module: subscribe `home/edge/+/reach`, upsert via `record_link(ok=None)`.
- Maps updated on both ends (`edge/MATRIX.md`/`MODULES.md`, `server/AGENTS.md`).

## Consequences

- The mesh **rebalances on real reach**, not just on already-exercised links — relocations and new nodes are
  discovered automatically, at the census cadence + dwell.
- The **fossil class is eliminated**: a `relay-none` or relocated node stays visible, so its coverage
  re-derives on its own. The one-time manual epoch bump becomes a legacy recovery path, not routine.
- Small, bounded extra MQTT traffic (one summary per node per interval) in exchange for full visibility — the
  opposite trade from `relay-all` (which restores visibility only by discarding the energy savings).
- Reach is **derived/soft state** — losing the census DB is never data loss; it repopulates within one
  interval. Assignments remain signed + epoch-guarded exactly as in ADR-0015.

## Rejected alternatives

- **`relay-all` census (status quo bootstrap).** Restoring visibility by making a node relay everything
  defeats the Phase-B energy savings — it is precisely the all-or-nothing the allowlist was built to avoid.
- **Routine manual epoch bumps.** Doesn't scale; the fossil recurs on every relocation/gating event; needs a
  human + a signed live-coordinator write each time.
- **Server-side inference only.** The coordinator cannot infer reach a node never reports; without a census it
  is structurally blind outside the current allowlist.
- **Drop the dwell / raise churn to chase reach.** Reintroduces flapping and burns epochs; the fix is better
  *observation*, not twitchier *actuation*.

## Open follow-ups

- Tuning knobs (start conservative, shadow-tune per ADR-0016): **census interval** ~5 min; **RSSI EWMA**
  time-constant a few samples; **reach retention / staleness** aligned with `adv_score` decay.
- Whether the census also piggybacks node health (heap/rssi-to-AP) to fold into one periodic report.
- Authenticate/rate-limit the `reach` topic (it influences assignment) — reuse the node cmd-secret posture.
- **Migration:** deploy census firmware fleet-wide, confirm the coordinator sees parity with relayed reach,
  then treat the census as the primary reach source; the `relay-all` bootstrap becomes redundant.
- `c6-bench` gets the **tactical epoch reset now** (see `c6-shared-migration-ota`) so the bedroom relay
  returns today; this ADR is the systemic fix so it never recurs.
