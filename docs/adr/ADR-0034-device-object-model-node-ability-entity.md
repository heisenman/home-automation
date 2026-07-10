# ADR-0034 — Device object model: Node / Ability / Entity across two planes

**Date:** 2026-07-10
**Status:** **Accepted** — object-model spine; Phase-0 primitives **resolved with Hugh 2026-07-10** (see the
Resolved section). Phases 1–2 **shipped** (reference doc + `classify()` re-frame closing
`device-push-actuator-class`); no registry migration (Phase 3 deferred).
**Builds on:** ADR-0002 (trait capability contract), ADR-0012 (transport-agnostic comms events),
[`docs/CONFORMANCE.md`](../CONFORMANCE.md) (the **ability catalog** — this ADR names the object model that catalog
already assumes), ADR-0021/0025 (the by-location / by-capability navigation axes).
**Related:** ADR-0001 (dictator owns the registry + MAC→identity mapping), ADR-0015 (edge relay / identity),
ADR-0019 (`roles:` profile), ADR-0026/0027 (area taxonomy + the actuator-area contract), ADR-0028 (migration
pending-hold), the `device_push` classifier (`server/maintenance/device_push.py`) and the
`device-push-actuator-class` task this ADR re-frames.

## Context — "device" is three different things wearing one word

The system has grown **three navigation axes** — by-location ([`AGENTS.md`](../../AGENTS.md) tree, ADR-0021),
by-capability ([`REUSE.md`](../REUSE.md), ADR-0025), and by-contract ([`CONFORMANCE.md`](../CONFORMANCE.md),
the ability catalog) — and a mature **ability** vocabulary that already says the important thing:
*a device is a **bundle of abilities**, and its obligations follow **function, not name**.* What it has **never
named** is the object model those axes all silently assume. The word "device" is overloaded across three
distinct things:

- a **physical unit** with firmware/radio/power (what you flash, repoint, fail over) — e.g. `coffice_c6`;
- a **logical, app-addressable thing** placed in a room (what the UI, rooms, storage, alerts key on) — e.g.
  `gas_c_office`, `meter_pro_living_room`;
- a **function** it performs (what determines its category, traits, and contracts) — "senses gas", "relays BLE",
  "actuates a fan".

These are routinely **not 1:1**, and the conflation is not academic — it produces real bugs:

1. **`device_push.classify()`** returns a flat enum `{tasmota, esp32, ble, unknown}` that is *half a transport*
   (`tasmota` ≈ WiFi-MQTT, `ble` ≈ a link) and *not a description of what the device is at all*. LAN actuators
   (Midea dehumidifier, Levoit purifier) are marked **`node: server`** in `control.yaml` — which is **not a
   transport** ("server" means *the driver runs on the dictator*, not on remote firmware). They classify as
   `unknown` and abort migration; the Midea move had to be finished by hand (`set_pending`). This is the
   `device-push-actuator-class` task — a symptom, not a root cause.
2. **Dual-role is the norm, not the exception.** The Levoit *senses* (pm25/aqi/filter_life) **and** *actuates*
   (fan/speed/LED). Every edge C6/S3 node *senses* its own I2C gas chip **and** *relays* other devices' BLE
   adverts it does not own. The D1001 is A+D+E+F+H+I+J at once ([`CONFORMANCE.md`](../CONFORMANCE.md) worked
   example). A model with one `device_type` per "device" cannot express any of them.
3. **Relays make the physical↔logical mapping many-to-many.** A meter's readings can arrive via node A *or*
   node B (ADR-0015 dedup). So an Entity is bound to *whichever Node relayed it this window* — there is no
   single owning node. Any model that assumes one device = one node = one reading source breaks here.

`CONFORMANCE.md` solved this for **obligations** (index by ability). This ADR does the same for **structure**:
name the objects, so migration keys off the physical thing, category keys off the function, and placement keys
off the logical thing — each concern reading the primitive it actually cares about.

## Decision — three primitives, two planes, one pivot

Adopt a single object model. **We reuse the existing `ability` vocabulary verbatim** (Principle 1); this ADR
adds only the two identity anchors the ability catalog already presumes.

| Primitive | Is | Keyed by | Lives in | Who reads it |
|---|---|---|---|---|
| **Node** | **any distinct physical unit** — edge firmware, a LAN appliance, or the dictator box itself (radio / power / an OS process) | `node_id` + a transport address (MAC / Tasmota topic / ESPHome name / eFuse) | the **Transport plane** | migration, repoint, OTA, failover, `classify()`, reachability |
| **Ability** | one *function* a Node performs — the [`CONFORMANCE.md`](../CONFORMANCE.md) catalog A–K (senses, actuator-traits, signed-directives, relays-BLE, display, battery, clock, data-of-record, OTA, comms-events, cluster-box). **Originating** abilities (sense/actuate) compose an Entity; the **relay** ability *carries* foreign Entities across a transport | `(node, ability-kind)` | the **edge between the planes** | category, traits/metrics, conformance obligations, policy |
| **Entity** | a logical, app-addressable thing = a **bundle of its originating Abilities**, placed in a room | `device_id` + `area` | the **Semantic plane** | UI, `/api/v1/*`, storage (`device_last_seen`, readings), rooms, alerts |

**The one-line spine:**

> A **Node** *hosts* **Abilities**. An **Entity** *is composed of* the Abilities that **originate** it — its
> identity and signals. An **originating Ability** is therefore the **join** where exactly one Node meets one
> Entity (the Levoit's *actuate* ability belongs at once to node `levoit-office` and to entity
> `purifier_living_room`). A second, non-originating flavor — **relay** — is hosted by a Node but *carries*
> foreign Entities across a transport (BLE→MQTT) without owning them.
> The Node is bound to the **Transport plane** (how bits move); the Entity to the **Semantic plane** (what the
> thing means + where it lives); the **Ability is the typed edge** between them — and it is where *category*
> lives, never on the Node.

### The two planes

- **Transport plane** (Node-side): `fabric → link/transport → node-address`. Household net `.0/.200` vs air-gap
  net `.1.0/.1.200`; BLE-adv / WiFi-MQTT / I2C-local / GATT / LAN-CLI / sysfs. **Transport is already just a
  metadata tag** in this system — every ingest path normalizes to the one canonical shape
  `home/<area>/<device_id>/state`, so transport does *not* determine identity or routing. This plane owns
  reachability, migration, repoint, OTA, and failover. **`classify()` belongs here.**
- **Semantic plane** (Entity-side): `entity → area → signals (metrics|traits) → presentation/policy`. This
  plane owns the UI, room composition (ADR-0026), storage, control strategy, and alerts. It is **transport-
  blind** by design (a dew point does not care whether it arrived over BLE or WiFi).

The planes **pivot through the Ability**: `device_id` (an Entity) exists because some Ability (e.g. "senses
gas") is exercised by some Node. That indirection is exactly what a flat "class" collapsed.

### The picture

```
            TRANSPORT PLANE                                     SEMANTIC PLANE
       how bits move · node-side                           what it means · entity-side
  migration·repoint·OTA·failover·classify            UI·rooms·storage·alerts·control·policy
  ══════════════════════════════════╗              ╔══════════════════════════════════════
                                     ║   ABILITY    ║
                                     ║  (typed join ║
     ┌──────────────┐                ║   / edge)    ║             ┌────────────────────┐
     │     NODE     │ ── hosts ──▶  ● senses-gas ──── originates ──▶│      ENTITY        │
     │  coffice_c6  │ ── hosts ──▶  ● relays-BLE ─┐  ║             │   gas_c_office     │
     │  WiFi-MQTT   │ ── hosts ──▶  ● signed-dir  │  ║             │   area: c_office   │
     │ class:       │ ── hosts ──▶  ● clock/OTA…  │  ║             └────────────────────┘
     │  edge-signed │              (its functions)│  ║ carries      ┌────────────────────┐
     │  node_id+MAC │                             └──╫───────────▶  │ meter_* (FOREIGN:  │
     └──────────────┘                                ║              │ owned by its own   │
      a physical unit                                ║              │ node, not this one)│
                                                     ║              └────────────────────┘
   ● originating ability = 1 Node : 1 Entity   ── the 1:1:1 pivot the flat "class" collapsed
   ● relay (carrying)    = 1 Node : N foreign Entities  ── bridges a transport, owns none

  Dual-role lives in ONE entity — an Entity *is* its originating abilities:

     ENTITY purifier_living_room ─┐
       ├─ senses      (pm25/aqi)  ├─ both hosted by ONE Node (levoit-office),
       └─ actuator-traits (fan)  ─┘  composed into ONE Entity

  Which question reads which primitive (the axis-dependence):
     "how do I move / flash / fail it over?"  → NODE     (transport plane)
     "what is it, what must it obey?"         → ABILITY  (the edge — category lives HERE)
     "where does it live, how is it shown?"   → ENTITY   (semantic plane)
```

### Cardinalities that fall out (and that the old model couldn't hold)

- **Node → Ability:** one-to-many. A Node is a *bundle* of abilities (the Levoit = senses + actuates; a C6 =
  senses-gas + relays-BLE + signed-directives + clock + OTA + comms-events).
- **Entity = its originating Abilities:** an Entity *is* the bundle of the abilities that originate its identity
  and signals (`purifier_living_room` = the Levoit's sense-ability + actuate-ability). One entity, one-or-more
  originating abilities.
- **Originating Ability ↔ (Node, Entity):** **1:1:1** — each originating ability pins exactly one Node to
  exactly one Entity. This is the join the flat "class" collapsed into a single word.
- **Relay (carrying) Ability → Entity:** one-to-many, and it *carries* rather than *originates* — one Node's
  relay ability delivers the signals of many **foreign** Entities (each originated by *its own* node's sensing
  ability) across a transport boundary. It owns none of them.
- **Entity → Node:** therefore **many-to-many and time-varying** — an Entity is pinned to its owning Node(s) by
  its originating abilities, and *additionally* served by whichever relay Node(s) carried it this window (the
  ADR-0015 preferred-source pick), with no single fixed owner for a relayed sensor.

## How this reconciles what already exists (nothing is thrown away)

| Existing thing | Is really about | Primitive it keys on |
|---|---|---|
| `edge/*/nodes.yaml` (node_id ↔ eFuse MAC, ADR-0020) | the physical unit + its transport binding | **Node** |
| `classify()` / repoint / OTA / `device_migrate` peer resurrection | operating on the physical unit | **Node** (Transport plane) |
| `CONFORMANCE.md` ability catalog A–K, ADR-0002 traits, ADR-0019 `roles:` | what a node *does* + what it must obey | **Ability** |
| `devices.yaml` / `tasmota-devices.yaml` / `levoit-devices.yaml` (address → `device_id`) | binding a transport address to a logical id | the **Node→Entity** resolution (the registries are the join table) |
| `control.yaml` (`device_id` → area + traits) | the logical actuator + its semantic placement | **Entity** (+ its actuator Abilities) |
| `areas.yaml` (ADR-0026), `/api/v1/rooms`, `device_last_seen.area` | where the logical thing lives | **Entity** (Semantic plane) |
| `METRIC_CATALOG`, `/api/v1/sensors`, dew-point derivation | the signals a sensing Ability produces | **Ability → Entity** signals |
| ADR-0012 comms events (`reachable | stale | acked | …`) | transport-agnostic health of a Node/Entity | spans the pivot (already transport-blind — the model it wanted) |

The three navigation axes stay exactly as they are; this ADR is the **fourth, structural axis** they hang on —
*"is this fact about the physical unit, the function, or the logical thing?"*

## The `classify()` fix (the concrete first consequence)

Re-frame `classify()` as a **Node/Transport-plane classifier** answering one honest question: *"what
repoint/migration procedure does this physical unit need?"* Its enum becomes transport-shaped and complete:

| Node class | Repoint procedure | Examples |
|---|---|---|
| `mqtt-broker` | rewrite the device's broker URI (Tasmota NVS / ESPHome) | Tasmota plugs, Levoit ESPHome |
| `edge-signed` | signed `repoint` directive to `home/edge/<node>/cmd` (ADR-0010) | C6/S3 gas + relay nodes |
| `ble-passive` | no device repoint — the dictator scans; ensure a relay covers the area | SwitchBot meters, Aranet |
| `local-driver` | **no network repoint** — driver already runs on the destination dictator; the physical WiFi move is a **manual app/flash step**, then confirm + pending-hold | Midea dehumidifier, Levoit purifier, host LEDs |

`local-driver` is the row that was missing — and it is a *transport* class (how the node is driven + moved),
not a claim about what the device is. That is the whole `device-push-actuator-class` task, now derived from the
model instead of bolted on: LAN actuators get `confirm_on_ha2` + the ADR-0028 pending-hold, and skip repoint
because there is no broker URI to rewrite — the move is manual by nature.

## Worked examples

- **Levoit purifier** — **Node** `levoit-office` (WiFi-MQTT, `local-driver`). **Abilities:** senses (A),
  actuator-traits (B), comms-events (J). **Entity:** `purifier_living_room` — one entity **composed of** a
  senses ability (pm25/aqi) + an actuator-traits ability (fan/LED); area authoritative from `control.yaml`
  (ADR-0027).
- **Edge C6 node** — **Node** `coffice_c6` (WiFi-MQTT, `edge-signed`). **Abilities:** senses-gas (A),
  relays-BLE (D), signed-directives (C), clock (G), OTA (I), comms-events (J). **Entities:** `gas_c_office`
  (composed of its own senses-gas ability) — **and** its relay ability *carries* `meter_*` entities (each
  originated by *its own* node's sensing ability), which `coffice_c6` does not own.
- **Midea dehumidifier** — **Node** `dehumidifier_living_room` (Midea LAN-CLI, `local-driver`). **Abilities:**
  actuator-traits (B); its onboard RH is a non-authoritative senses (A). **Entity:** `dehumidifier_living_room`.
- **host_210** — **Node** = the dictator box itself (sysfs, `local-driver`). **Ability:** actuator-traits (B,
  indicator). **Entity:** `host_210`. (Same box also exercises cluster-box (K) + data-of-record (H) abilities —
  the object model makes "the server is also a controllable device" expressible instead of anomalous.)

## Rollout (name it now; conform in windows — mirrors ADR-0026)

- **Phase 0 — bless the primitives (this ADR). ✅ Resolved 2026-07-10** — Node / Ability / Entity + the two
  planes + adopting the `ability` vocabulary agreed with Hugh (see the Resolved section).
- **Phase 1 — the reference doc + AGENTS wiring (dev, cheap, safe).** Land `docs/DEVICE-MODEL.md` (the explainer
  this ADR points to) and a short pointer from `AGENTS.md` / `docs/AGENTS.md` — the fourth navigation axis.
  No behavior change.
- **Phase 2 — the `classify()` re-frame (dev). ✅ Done 2026-07-10.** `device_push.classify()` now returns the
  Node-class enum (`mqtt-broker`/`edge-signed`/`ble-passive`/`local-driver`, +`unknown`); the `local-driver`
  procedure skips repoint (manual move) and flows into confirm + pending-hold — **closing
  `device-push-actuator-class`**. A dry-run push of `dehumidifier_living_room` now completes the full state
  machine instead of aborting at `unknown`; guarded by `tests/test_device_push.py` (41 migration-suite tests
  green). *Caveat:* `confirm_on_ha2` polls `/api/v1/sensors` (authoritative-sensors-only), so a pure actuator
  may not surface there — the existing `migration_activate.py check` fallback (device_last_seen, any device) is
  the authoritative post-move gate (FOLLOWUPS). The two live LAN actuators already migrated by hand, so this is
  forward-proofing + failover-rebuild correctness (a rebuild re-pushes every device). **Committed but NOT yet
  deployed to ha-2** (air-gap) — tracked with a code tripwire + sync test in
  [`docs/airgap/HA2-DEPLOY-PENDING.md`](../airgap/HA2-DEPLOY-PENDING.md) so a rebuild-from-ha-2 that hits the
  drift is routed to *deploy, don't re-implement*.
- **Phase 3 — optional registry/vocabulary convergence (gated, later; deferred per Phase-0 #4).** Where the registries carry ambiguous
  fields (e.g. `levoit-devices.yaml` area post-ADR-0027, the synthetic gas-key aliases), align them to
  "registries are the Node→Entity join table" — a cleanup, not a rewrite. This folds into the CONFORMANCE.md
  "declare abilities in the registry" follow-on so ability-declaration and Node/Entity identity land together.

## Resolved in Phase 0 (Hugh, 2026-07-10)

1. **Physical-unit term = `Node`** — adopted, and it spans **any distinct physical unit**: edge firmware, a LAN
   appliance, and the dictator box itself (zero new vocabulary — `node_id` / `nodes.yaml` / `node:` are already
   in use).
2. **`relay` is an Ability; `BLE` is a Transport** — and this does **not** strain the model; it sharpens it.
   `relay` is a *carrying* ability: it bridges transports (BLE-in → MQTT-out) and delivers foreign entities
   without owning them — a distinct flavor from the *originating* abilities (sense/actuate) that compose an
   entity. The transport(s) a relay bridges are Transport-plane parameters of that ability instance; since an
   Ability sits on the edge between planes, it is the one place allowed to name a transport. BLE stays a
   transport, relay stays an ability, no layer is crossed.
3. **An Entity *contains* its Abilities** — confirmed; the first draft had the arrow backwards ("Ability
   surfaces Entity"). Corrected throughout: **a Node *hosts* abilities; an Entity *is composed of* its
   originating abilities; the Ability is the join** where a hosting Node and a composed Entity meet (the
   Home-Assistant *Device→Entity* / Matter *Node→Endpoint→Cluster* containment). Default granularity stays
   **one Entity per physical appliance**; the model already supports splitting later (Ability→Entity is
   one-to-many) if e.g. a power strip ever needs one entity per outlet.
4. **Registry/vocabulary convergence (Phase 3) — deferred** until the model has proven itself further along
   (after the `classify()` re-frame); the registries keep working unchanged meanwhile.

## Consequences

- **Every fact has an unambiguous home.** "How do I move it?" → Node. "What is it / what must it obey?" →
  Ability. "Where does it live / how is it shown?" → Entity. The `classify()`, `node: server`, and dual-role
  confusions cannot recur because the questions no longer share a word.
- **`device-push-actuator-class` is absorbed**, not special-cased — it becomes the `local-driver` Node class.
- **A failover rebuild becomes correct**, because re-pushing devices keys off Node class (a transport-plane
  operation) with a complete enum — the one scenario where the missing actuator class actually bites.
- **Reuse-first honored:** the model adds *two* named anchors (Node, Entity) around the ability catalog the repo
  already had; it introduces **no** competing "capability" term.
- **Acceptance is descriptive first, enforced later:** Phase 1 is a doc that makes the model teachable; Phase 2
  proves it by deleting a class of bug from `classify()`. No big-bang migration — the registries keep working
  unchanged and converge only if/when Phase 3 is blessed.
