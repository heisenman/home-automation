# ADR-0015 — Edge relay coverage assignment & live-adv dedup (preferred-relay directives)

Status: **Proposed** (2026-06-24) — plan for review before implementation. Extends
[ADR-0010 mesh-topology-routing](ADR-0010-mesh-topology-routing.md) and
[ADR-0001 dictator-authority-model](ADR-0001-dictator-authority-model.md).

## Context

As edge relays multiply, every relay forwards **everything** it hears. Today an edge node (the C6 design,
the new `edge/esp32s3-eth/` node) blindly publishes every decoded advert to `home/edge/<node>/<mac>/adv`;
`ha-edge-mapper` republishes all of them to `home/<area>/<id>/state`; **and** the dictator's own onboard
scanner publishes the same meters *directly*. Observed live 2026-06-24: the S3 node relayed **9 meters that
210 already hears locally** → the same readings arrive 2× (3× with another relay). This scales badly:
redundant MQTT traffic, and wasted edge **radio/CPU/PoE energy** transmitting readings a closer source
already delivers. There is **no relay preference and no server↔edge path negotiation** — the gap Hugh flagged.

**What already exists (do not reinvent):** ADR-0010 built a topology graph that answers *"which receiver
reaches which meter best"* — `mesh_links(rssi,n_ok,n_fail,…)`, `best_path()`/`hops()` Dijkstra over
reach+reliability, passively fed from the very `meta.rssi`/`node` each advert already carries. ADR-0010
used it for **history-backfill PULL routing**; this ADR reuses the same graph for **live-adv source
selection** and adds the directive channel to act on it.

## Decision

Centrally assign a **single preferred source per meter** from the topology graph, and enforce it in two
tiers under one unifying model. The dictator owns the assignment (ADR-0001); edge nodes obey directives.

### Unifying model — every receiver is a node
Treat the dictator's onboard scanner as node **`local`** in the topology, so *all* receivers (local +
each edge node) are uniform vertices and every meter resolves to exactly one preferred source. This also
fixes the edge-vs-local wrinkle (below) by funnelling all readings through one dedup point.

### Preferred-source selection (`server/mesh/`)
- Add **`best_relay(graph, endpoint)`** — a relay variant of `best_path` that scores live-adv reach
  (link kind + RSSI + failure ratio + staleness) but **skips the pull-history `_terminal_adjust`** (that
  bonus/penalty is about GATT pulls, irrelevant to passively hearing an advert). `hops==0` → `local` is
  preferred; `hops==1` → that edge node; `hops≥2` → deferred (multi-hop, ADR-0010 Phase 3).
- **Hysteresis (sticky):** switch a meter's assigned source only if a challenger beats the incumbent by
  ≥ `SWITCH_MARGIN` (≈6 dB / equivalent cost) sustained ≥ `SWITCH_DWELL_S`. No flapping on RSSI noise —
  same spirit as the failover primary-supremacy debounce.
- **Failover:** if the assigned source goes silent > `FRESH_WINDOW_S`, demote it and promote next-best.

### Tier 1 — server-side dedup (NO firmware change; immediate data-side win)
`ha-edge-mapper` consults the assignment and republishes a meter **only** from its preferred source,
dropping the rest (with stale-source failover). Edge-vs-local is solved by the model above:
- **Recommended:** the `local` scanner publishes via `home/edge/local/<mac>/adv` so *all* readings pass
  through the mapper's single dedup gate. Uniform, one code path.
- **Alternative:** keep `local` direct; dedup at the writer by `(device_id, metric, ~time-bucket)` keeping
  the best-RSSI source. Simpler to add, but two dedup paths and time-bucket fuzz.
- Gated + rollback-safe like ADR-0010 (`--relay-dedup` / env, default OFF until validated).
- **Effect:** kills duplicate canonical-state writes now. Edge nodes still transmit everything (no energy
  save yet — that's Tier 2).

### Tier 2 — edge relay directives (firmware; saves edge energy)
The dictator computes each node's **relay allowlist** (the meters it's the preferred source for) and
pushes a **retained** directive on the existing downlink `home/edge/<node>/cmd` (or a dedicated
`home/edge/<node>/relay`):
```json
{"schema":1, "type":"relay_assign", "epoch":7, "relay_macs":["B0:E9:FE:54:AB:A2", "..."], "ttl_s":3600}
```
- Node: persists the allowlist in **NVS** (recorded, survives reboot), filters its relay so it publishes
  only assigned MACs, applies updates (epoch-guarded → idempotent/ordered). Retained so a rebooting node
  immediately re-reads its current assignment.
- **Default before the first directive: relay-all** (today's behavior) — an un-provisioned node is useful
  immediately and the dictator *tightens* it once coverage is learned. Backward-compatible.
- **Effect:** edge nodes stop relaying redundant meters → real radio/CPU/MQTT energy saved.
- **Integrity:** reuse the ADR-0010/command-control HMAC signing if/when the broker carries authority;
  optional on today's trusted-LAN anonymous broker.

### Bidirectional loop (both directions, as Hugh asked)
- **Uplink (edge→server):** every `{node, mac, rssi}` advert → `record_link()` into `mesh_links`. The
  coverage map is learned **passively + continuously** (already partly done by `tools/mesh_probe.py`;
  make `edge_mapper` record links live so it's always fresh).
- **Downlink (server→edge):** the `relay_assign` directive.
- **Re-negotiation (updateable):** recompute on node up/down + on sustained coverage shift, plus a slow
  periodic sweep; re-push only changed assignments (debounced).

## Components & changes
| Area | File | Change |
|------|------|--------|
| Topology | `server/mesh/topology.py` | add `best_relay()` (or `best_path(…, for_relay=True)`) |
| Assignment | `server/mesh/assign.py` (new) | `{mac→source}` + `{node→relay_macs}`, hysteresis, failover, persist current |
| Tier-1 dedup | `server/ingest/edge_mapper.py` | preferred-source gate + live `record_link()` |
| Local-as-node | `server/ingest/scanner.py` | (recommended) publish via `home/edge/local/<mac>/adv` |
| Coordinator | `ha-relay-coordinator` (new svc) or fold into ha-controller | compute + publish directives (Tier 2) |
| Firmware | `edge/esp32*/main` (`ble_scan`/`ha_mqtt`/`ha_config` + a cmd handler) | NVS allowlist + relay filter |
| Schema | directive JSON (+ optional HMAC) | `relay_assign` contract above |

## Phasing
- **Phase A (Tier 1):** `best_relay` + assignment + mapper dedup + local-as-node. No firmware. Gated, default
  off, static/passthrough fallback. Immediate duplicate-write kill.
- **Phase B (Tier 2):** directive protocol + firmware relay filter + coordinator. Per-node rollout; saves
  edge energy. Default-relay-all keeps un-updated nodes safe.
- **Phase C:** signing, re-negotiation tuning, and converge with ADR-0010 Phase 3 (multi-hop relay transport)
  so the same graph drives both live-adv routing and backfill pulls.

## Consequences
- **+** Eliminates redundant traffic + edge energy; coverage becomes a managed, queryable, self-learning
  fact (one graph for both this and ADR-0010 backfills). **+** Reuses proven mesh code; gated rollout.
- **−** New firmware path (cmd/NVS/filter) to test per board — mitigated by default-relay-all. **−**
  Assignment flapping risk → hysteresis is mandatory, not optional. **−** Edge-vs-local needs the
  local-as-node change (or writer dedup) — a real decision. **−** Directive trust: fine on the trusted LAN;
  sign when the broker carries authority.

## Open questions (for review)
1. **Local scanner:** route-through-mapper as node `local` *(recommended — uniform)* vs writer-side dedup?
2. **Pre-directive default:** relay-all *(recommended — backward-compatible)* vs relay-none?
3. **Directive channel:** reuse `…/cmd` vs dedicated `…/relay`? Sign it now or defer to broker-auth?
4. **Coordinator home:** new `ha-relay-coordinator` service vs fold into `ha-controller`/`ha-api`?
5. **Re-negotiation cadence:** event-driven (node up/down, coverage shift) + slow periodic — what interval?
6. **Scope of v1:** ship Tier 1 alone first (data-side win, zero firmware risk), then Tier 2?
