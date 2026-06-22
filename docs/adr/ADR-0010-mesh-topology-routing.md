# ADR-0010 — Mesh topology graph + path-aware backfill routing

Status: Accepted (Phase 1 implemented 2026-06-22) — Phase 3 (multi-hop relay transport) deferred.

## Context

History backfills are dispatched by `tools/gap_watcher.py`, which until now chose a pull path with a
**static** rule: outdoor/meter → edge node `c6-bench`, close → server, aranet → aranet. Two problems
surfaced while chasing the c_office saga (2026-06-22):

1. **Reachability is rediscovered by hand, every time.** Working out "can the host reach this meter,
   or must an edge node, and which one" took a dozen ad-hoc probes — facts the system already half-sees
   (per-node adv RSSI, host RSSI in `device_last_seen`) but throws away.
2. **RSSI ≠ pull-ability.** `meter_pro_living_room` (A8:02) is the *loudest* meter to the host (−58) yet
   the host cannot pull it; the office meter (AE:6B) is near an edge node but weak to the host. Loudness
   alone routes wrong.

And the topology is **not a star**. Node backhaul (wifi/ethernet) is not guaranteed and BLE crosses
walls, so the real fabric can be a chain:

    server ──ip──> node_a ──espnow──> node_b ──ble──> endpoint

## Decision

Model the fabric as a **directed graph** and route backfills by **pathfinding**, not static rules.

### Graph (`server/mesh/topology.py`, pure + unit-tested)
- Vertices are `(kind, id)`: `('server','server')`, `('node', <id>)`, `('endpoint', <device_id>)`.
- `Link(src, dst, kind, rssi, n_ok, n_fail, age_s)`; `kind ∈ {ip, espnow, ble-adv, ble-gatt}`.
- `best_path()` = Dijkstra from the server to an endpoint, minimising summed `link_cost`.
- **Cost folds reach AND pull-history**: each hop costs by link type + RSSI penalty + failure ratio +
  staleness decay; the *terminal* receiver→endpoint hop is additionally adjusted by that receiver's
  **pull-outcome history** with that endpoint (`PULL_OK_BONUS` / `PULL_FAIL_PENALTY`). So a node that
  has actually pulled a device beats a louder receiver that has only failed — the A8:02 lesson, encoded.
- `hops(path)`: 0 = server pulls direct (own radio), 1 = one edge relay, ≥2 = needs multi-hop transport.

### Persistence (`server/mesh/store.py`, self-contained schema in hot.db)
- `mesh_links(src,dst,link_kind,rssi,n_ok,n_fail,last_ts)` — upserted observed edges.
- `pull_log(ts,device_id,path,ok,n_samples,reason)` — append-only pull audit; `pull_stats()` aggregates
  it by terminal receiver to feed the terminal cost adjustment.

### Observation (`tools/mesh_probe.py`, all passive — no new radios)
- host→endpoint from `device_last_seen.last_rssi`; node→endpoint from a live `home/edge/+/+/adv` sniff
  (every advert already carries node + `meta.rssi`); server→node `ip` proven by any message from a node.
- node↔node (`espnow`) is the only missing observer — added when ≥2 nodes exist and a node can report
  hearing another's beacon. The graph + pathfinder already handle the extra hop today.

### Routing (`gap_watcher.choose_plan`)
- Graph path → dispatch: `hops==0` server, `hops==1` edge-via-that-node, `hops≥2` **falls back to static**
  (multi-hop relay transport not built — see deferred). Empty/unknown graph → static. **Gated behind
  `--graph-routing` / `HA_GRAPH_ROUTING`, default OFF** → zero behavior change until validated.

## Deferred (Phase 3) — multi-hop relay transport

The graph can already *compute* a `server→node_a→node_b→endpoint` path, but **executing** one needs a
node-to-node command/stream relay (likely ESP-NOW or node→node MQTT-bridge) that does not yet exist.
Until it does, `choose_plan` reports such paths and falls back to static. This is the right seam: the
routing brain is done and tested; only the transport is pending hardware (more nodes arrive in weeks).

## Learning loop (Phase 2, partial)

`pull_log` is written by the pullers so the graph self-corrects (a failed path gets penalised next run).
Wiring `record_pull` into `tools/switchbot_history.py` and `server/ingest/edge_history.py` is the
remaining step to close the loop (kept out of this change set to avoid colliding with concurrent
Meter-Pro pull debugging on those files).

## Consequences

+ Reachability/pull-ability become **persisted, queryable facts**, not tribal knowledge re-derived per
  incident. + Routing is future-proof for multi-node/multi-hop without a data-model rewrite. + Safe
  rollout (gated, static fallback everywhere). − A second pass is needed to wire `pull_log` into the two
  pullers and to add the node↔node observer + relay transport when multi-node hardware lands.
