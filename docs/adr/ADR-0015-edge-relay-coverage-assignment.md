# ADR-0015 — Edge relay coverage assignment & live-adv dedup (preferred-relay directives)

Status: **Accepted** (2026-06-24) — 9 open questions resolved with Hugh (see *Decisions*, below);
implementation may proceed per *Phasing*. Extends
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

### Identity — assign over REGISTERED device_ids / node_ids, never raw MAC
The preferred map and the whole topology are keyed by **logical identity**, matching ADR-0010's vertices
`('endpoint', device_id)`, `('node', node_id)`, `('server','server')` — **registered devices and nodes, not
MAC addresses.** A MAC is only the BLE *transport address*, resolved from the registry by the dictator at the
point of enforcement (nodes stay dumb — ADR-0001). This makes three classes of target first-class:
- **Sensors** = endpoint `device_id` → *uplink* adv-relay routing (who hears it best).
- **Actuators** = endpoint `device_id` → ***downlink* command-relay routing** (which node best *reaches* it).
  Covers a BLE actuator sitting behind an edge node; LAN actuators (Midea) are `local`-direct but use the
  same map. So an assignment is **bidirectional**: best receiver for a sensor's adverts *and* best relay
  for an actuator's commands.
- **Edge nodes** = `node_id` vertices → **multi-hop daisy chains** `server→node_a→node_b→endpoint`. In a
  large house/property the fabric is a real graph, not a star — exactly ADR-0010's deferred Phase-3 multi-hop
  transport, now sharing one identity model and one graph.

A MAC appears ONLY in the per-node BLE-adv filter; the dictator resolves each assigned `device_id` → its
MAC(s) from the registry when it builds that node's directive. Command relay (downlink to actuators) is
addressed by `device_id` end-to-end (ADR-0010 command-control), no MAC at all.

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
The dictator computes each node's **assignment** (the endpoints it's the preferred relay for, by
`device_id`) and pushes a **retained** directive on the existing downlink `home/edge/<node>/cmd` (or a
dedicated `home/edge/<node>/relay`). `relay_macs` is the dictator's registry resolution of those
device_ids → BLE MACs (all the node's adv filter can match); a node that also relays commands to a BLE
actuator gets a parallel `cmd_relay` keyed by **device_id** (ADR-0010 command-control, no MAC):
```json
{"schema":1, "type":"relay_assign", "epoch":7,
 "relay_macs":  ["B0:E9:FE:54:AB:A2", "..."],            // resolved from assigned sensor device_ids
 "cmd_relay":   ["dehumidifier_hall", "..."],            // actuator device_ids this node relays cmds to
 "ttl_s":3600}
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
| Assignment | `server/mesh/assign.py` (new) | `{device_id→source}` + `{node→device_ids}` (MAC-resolved for BLE adv; device_id for cmd relay), hysteresis, failover, persist |
| Tier-1 dedup | `server/ingest/edge_mapper.py` | preferred-source gate + live `record_link()` |
| Local-as-node | `server/ingest/scanner.py` | (recommended) publish via `home/edge/local/<mac>/adv` |
| Coordinator | `ha-relay-coordinator` (new svc — decided) | compute + publish **signed** directives on `…/relay` (Tier 2) |
| Web config | PWA (`server/web` / API) | surface coverage assignment + registry edits (Hugh: user config lives in the web app) |
| Firmware | `edge/esp32*/main` (`ble_scan`/`ha_mqtt`/`ha_config` + a cmd handler) | NVS allowlist + relay filter |
| Schema | directive JSON (+ optional HMAC) | `relay_assign` contract above |

## Phasing
- **Phase 0 (failover transparency — prerequisite, low-risk):** repoint edge nodes (`broker_uri` / NTP /
  OTA host) **and** PWA/API clients to the **VIP `.200`**; confirm the broker answers on the VIP. Makes the
  data bus survive a dictator swap before any coverage logic is layered on. (Includes reflashing the S3.)
- **Phase A (Tier 1):** `best_relay` + assignment + mapper dedup + local-as-node. No firmware. Gated, default
  off, passthrough fallback. Immediate duplicate-write kill.
- **Phase B (Tier 2):** directive protocol + firmware relay filter + coordinator. Per-node rollout; saves
  edge energy. Default-relay-all keeps un-updated nodes safe. **Replicate `mesh_links`+assignments via
  `sync-standby`; recompute on `notify.sh master`.**
- **Phase C:** sensor-history reconciliation across failover (§transparency #2), `ha-api` on the VIP (#4),
  directive signing, re-negotiation tuning, and converge with ADR-0010 Phase 3 (multi-hop relay transport)
  so one graph drives both live-adv routing and backfill pulls.

## Failover transparency & state continuity (synthesis with the dictator-pair topology)

The dictator is a failover **pair** (210 primary ↔ .245 standby) behind VIP **192.168.0.200** (keepalived;
`failover/`, LIVE+tested 2026-06-24). ADR-0015 **builds on** that, it does not re-do it. The failover impl
already provides: the VIP floats with the dictator and `ha-controller` binds to its holder (`notify.sh`);
the cluster-coordination bus `ha/cluster/#` is bridged between the two brokers
(`failover/mosquitto/cluster-bridge.conf`) while **device telemetry `home/#` is deliberately NOT bridged**
(no loop/coupling); and `sync-standby.sh` (~30-min timer) replicates `midea-device.env`, `control.yaml`,
and `control.db`. For the **relay mesh + clients** to survive a dictator swap, four paths must close:

1. **Address the ROLE, not the box.** Everything that talks to the dictator must target the **VIP**, never
   210/.245. *Concrete gap, today:* the edge nodes (incl. the S3 just flashed) use `mqtt://192.168.0.210:1883`
   — repoint to `192.168.0.200`. Same for the **OTA host pin**, any node **NTP**, and the **PWA/API clients**.
   The broker already listens on `0.0.0.0`, so it answers on the VIP automatically — once nodes/clients use
   it, the data bus follows the dictator on failover with **zero per-node reconfig**. This is the immediate,
   low-risk fix and a prerequisite for the rest.

2. **Time-series continuity.** `sync-standby` replicates control state but **not** the sensor history
   (`hot.db`/parquet). Because live edge data follows the VIP to whichever box is dictator, the two boxes'
   histories **diverge by epoch**. Add **bidirectional reconciliation** (each box backfills the readings it
   missed during the other's reign) as a reconcile-on-promotion/return step, reusing the history-sync
   machinery (ADR-0007/0009); the writer's `(ts,metric)` dedup makes the merge idempotent.

3. **The reselection brain must survive the swap.** ADR-0015's inputs (`mesh_links`) + outputs (assignments)
   live in `instance/db/` → fold them into `sync-standby` (snapshot like `control.db`) so the standby
   inherits the coverage map. **AND recompute on promotion:** the new dictator's onboard radio + reachable
   nodes hear a *different* set, so `notify.sh master` should trigger an ADR-0015 reselection pass — a
   dictator swap is the largest topology change. *Replicate to seed; recompute to be correct.*

4. **The API/dashboard on the VIP.** `ha-controller` floats; the read/command API + PWA should too. Decide
   whether `ha-api` runs warm on the standby (reads always; control plane mounts on `notify.sh master`) with
   clients using the VIP — else the dashboard dies on failover even though control survived.

**Net:** the failover impl nailed the *control* plane; #1–#4 extend that transparency to the *data* plane
(edge mesh + history + clients) and to the coverage brain. They belong here because ADR-0015 is what makes
the mesh exist; #2 (history reconciliation) may graduate to its own ADR if it grows.

## Consequences
- **+** Eliminates redundant traffic + edge energy; coverage becomes a managed, queryable, self-learning
  fact (one graph for both this and ADR-0010 backfills). **+** Reuses proven mesh code; gated rollout.
- **−** New firmware path (cmd/NVS/filter) to test per board — mitigated by default-relay-all. **−**
  Assignment flapping risk → hysteresis is mandatory, not optional. **−** Edge-vs-local needs the
  local-as-node change (or writer dedup) — a real decision. **−** Directive trust: fine on the trusted LAN;
  sign when the broker carries authority.

## Decisions (resolved with Hugh, 2026-06-24)
1. **Local scanner → route-through-mapper as node `local`.** All readings (local + every edge node) pass one
   dedup gate via `home/edge/local/<mac>/adv`. Uniform, single code path.
2. **Pre-directive default → relay-all.** An un-provisioned node behaves as today; the dictator only ever
   *narrows* coverage once learned. Backward-compatible.
3. **Directive channel → dedicated `home/edge/<node>/relay`, signed now** (HMAC, reusing the OTA/command
   signing infra). A directive that changes what a node ignores warrants the same integrity as a command;
   cheap now, awkward to retrofit.
4. **Coordinator home → Tier-1 dedup lives in `ha-edge-mapper`; the Tier-2 directive coordinator is its own
   `ha-relay-coordinator` service.** Per Hugh, services are cheap (fast boxes + a planned OS/critical-service
   optimization pass), so we don't contort to avoid a daemon — a dedicated coordinator is cleaner to reason
   about and to fail over.
5. **Re-negotiation cadence → event-driven (node up/down, sustained coverage shift) + a slow ~15-min periodic
   backstop**, re-pushing only changed assignments (debounced). Timings are dev's to tune from live data
   (Hugh's deferral); start at 15 min and adjust on observed churn.
6. **v1 scope → Tier 1 first** (server-side dedup/assignment, zero firmware change), validate, then Tier 2
   (firmware directives). Captures the data-side win with no firmware risk.
7. **VIP rollout → clients/dashboard repoint to VIP `.200` now (wired, VIP reachable); edge nodes fold into
   the next firmware update, NOT a reflash-now.** The S3 is parked on `.210` precisely because the wifi
   segment can't ARP the VIP — repointing wifi nodes today would break them. Wired future nodes default to the
   VIP; the wifi-VIP gap is fixed at the router (`openwrt-router-onboard`), not by reflashing.
8. **History reconciliation → its own ADR; reconcile-on-promotion** (snapshot-merge over the **cluster
   back-channel**, not the public bus/GitHub), not continuous time-series replication. A bounded gap during
   the rare swap window is acceptable for sensor history; the writer's `(ts,metric)` dedup makes the merge
   idempotent.
9. **`ha-api` on the standby → warm read-only + mount-control-on-promote.** Reads/dashboard stay up during an
   outage; control endpoints stay unmounted until `notify.sh master`, preserving the one-controller invariant.

### Cross-cutting clarifications (Hugh) — apply repo-wide, not just here
- **Aggregation is dictator-central.** All telemetry aggregates on the dictator; history syncs over an
  **RPC/cluster back-channel** (the `ha/cluster/#` bridge + `/cluster/*` RPC + ssh), never `home/#` or GitHub.
- **Backup nodes that are *also* edge devices still relay up.** The incoming display hardware (not yet on-site)
  may be both a standby participant and an edge relay; being a backup does not exempt it from central
  aggregation — its readings still flow to the dictator like any edge node.
- **User-configurable = web-app surface.** Anything a user sets now or later (coverage assignment, device
  registry, relay tuning) belongs in the PWA, not sneakernet YAML or CLI. New config introduced by this ADR
  must land a web-app control, not a hand-edited file.
