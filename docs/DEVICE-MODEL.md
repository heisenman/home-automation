# DEVICE-MODEL.md — Node / Ability / Entity: the object model, explained

**The fourth navigation axis.** The repo routes **by-location** ([`AGENTS.md`](../AGENTS.md) tree, ADR-0021),
**by-capability** (["what can I reuse"](REUSE.md), ADR-0025), and **by-contract** (["what must it obey"](CONFORMANCE.md),
the ability catalog). This is the **by-structure** axis: *"is this fact about the physical unit, the function,
or the logical thing?"* It is the plain-language companion to the decision record in
[ADR-0034](adr/ADR-0034-device-object-model-node-ability-entity.md) — read this to **understand** the model;
read the ADR for the **why** and the resolved trade-offs.

> **The one sentence:** A **Node** *hosts* **Abilities**; an **Entity** *is composed of* its originating
> **Abilities**; the **Ability** is the join where one Node meets one Entity — and it is where *category* lives,
> **never** on the Node.

## Why "device" wasn't enough

The word "device" was doing three jobs at once, and they are routinely not 1:1:

- what you **flash / repoint / fail over** — a *physical unit* (`coffice_c6`),
- what the **app / rooms / storage / alerts** address — a *logical thing* (`gas_c_office`),
- what determines its **category / traits / contracts** — a *function* ("senses gas", "relays BLE").

A dehumidifier that also reports humidity, an edge node that senses gas *and* relays other rooms' meters, a
panel that displays *and* relays *and* watches its own battery — none of these fit "one device, one type". So
we name the three things separately.

## The three primitives

| Primitive | Plain definition | Keyed by | Examples |
|---|---|---|---|
| **Node** | **any distinct physical unit** — edge firmware, a LAN appliance, a Tasmota plug, or the dictator box itself. The thing with a radio/power/an OS process that you migrate, OTA, and fail over. | `node_id` + a transport address (MAC / Tasmota topic / ESPHome name / eFuse) | `coffice_c6`, `levoit-office`, `plug_g11`, `host_210` |
| **Ability** | one **function** a Node performs. The set is closed — the [`CONFORMANCE.md`](CONFORMANCE.md) catalog **A–K** (senses, actuator-traits, signed-directives, relays-BLE, display, battery, clock, data-of-record, OTA, comms-events, cluster-box). **Category, traits, metrics, and obligations live here.** | `(node, ability-kind)` | "senses-gas", "relays-BLE", "actuator-traits" |
| **Entity** | a **logical, app-addressable thing** placed in a room = a **bundle of its originating abilities**. What the UI, `/api/v1/*`, `device_last_seen`, rooms, and alerts key on. | `device_id` + `area` | `gas_c_office`, `meter_pro_living_room`, `purifier_living_room` |

## Two planes, one pivot

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
```

- **Transport plane** (Node-side) owns *how bits move*: which net (household `.0/.200` vs air-gap `.1.0/.1.200`),
  which transport (BLE-adv / WiFi-MQTT / I2C-local / GATT / LAN-CLI / sysfs), and every operation on the
  physical unit — migration, repoint, OTA, failover, reachability, `classify()`.
- **Semantic plane** (Entity-side) owns *what it means*: area (ADR-0026), room composition, storage, control
  strategy, alerts. It is **transport-blind** — a dew point does not care whether it arrived over BLE or WiFi.
- **The Ability is the edge between them.** It is the one place allowed to name a transport (a "senses-gas"
  ability rides I2C; a "relays-BLE" ability bridges BLE→MQTT). Category lives here because *obligations follow
  function, not name* (the CONFORMANCE.md principle).

### Originating vs carrying abilities (why relays don't break the model)

- An **originating** ability (**sense** / **actuate**) *produces* an entity's own signals. It is a **1:1:1**
  join: exactly one Node, exactly one Entity. An Entity **is** the bundle of its originating abilities.
- The **relay** ability is **carrying**, not originating: one Node's relay *delivers* the signals of many
  **foreign** entities (each originated by *its own* node's sensing ability) across a transport boundary. It
  owns none of them. This is why an Entity's binding to a Node is many-to-many and time-varying — a meter is
  served by whichever relay heard it this window (the ADR-0015 preferred-source pick).

## Reading the model — which question reads which primitive

| You're asking… | Read the… | Because it's a… |
|---|---|---|
| "how do I move / flash / repoint / fail it over?" | **Node** | transport-plane operation |
| "what is it? what traits/metrics? what must it obey?" | **Ability** | function → category + contract |
| "where does it live? how is it shown? what's stale?" | **Entity** | semantic-plane fact |

If a question makes you reach for two primitives at once, you've found a conflation — split it.

## Worked catalog — every live device class, mapped

Each real class in the house, in `Node (class) → Abilities → Entity(ies)` form:

| Physical thing | **Node** (transport / repoint class) | **Abilities** (CONFORMANCE A–K) | **Entity(ies)** |
|---|---|---|---|
| SwitchBot meter | `ble-passive` (MAC; no device repoint — dictator/relays scan it) | senses (A) | `meter_*` — *delivered* by whichever relay/scanner heard it |
| Aranet radon | `ble-passive` (extended-adv MAC) | senses (A) | `radon_crawlspace` — dual-heard (scanner + S3/C6 relays), redundancy by design |
| Edge C6 gas node | `edge-signed` (`node_id`+eFuse; signed repoint directive) | senses-gas (A), relays-BLE (D), signed-dir (C), clock (G), OTA (I), comms (J) | owns `gas_c_office`; **carries** the `meter_*` it relays |
| Edge S3-ETH node | `edge-signed` (ETH/WiFi) | senses-gas (A), relays-BLE (D)+Aranet decode, signed-dir (C), clock (G), OTA (I), comms (J) | owns `gas_kitchen`; carries meters + radon |
| Tasmota plug/meter | `mqtt-broker` (Tasmota `%topic%`; rewrite broker URI in NVS) | senses (A, energy) | `plug_g11` |
| Levoit purifier | `local-driver` (ESPHome; manual reprovision, server-side driver) | senses (A, pm25/aqi), actuator-traits (B, fan/LED), comms (J) | `purifier_living_room` (one entity = sense + actuate) |
| Midea dehumidifier | `local-driver` (LAN-CLI; manual app move, server-side driver) | actuator-traits (B), senses (A, onboard RH — non-authoritative) | `dehumidifier_living_room` |
| Dictator box LEDs | `local-driver` (sysfs via sudo) | actuator-traits (B, indicator) | `host_210` — same box also does data-of-record (H) + cluster-box (K) |
| D1001 / E1001 panel | `edge-signed` / ops-MQTT | display (E), relays-BLE (D), battery (F), data-of-record (H)†, OTA (I), comms (J), (E1001) senses own battery | panel entity + carried meters (†panel is not a signing node — see CONFORMANCE) |

*Forward note:* `local-driver` is today's honest bucket for ESPHome-over-WiFi appliances (Levoit) because their
network move is a manual reprovision. If a scripted ESPHome broker-repoint is ever built, they graduate toward
`mqtt-broker` — a Node-class change with **no** effect on their abilities or entity. That independence is the
point.

## The registries are the Node→Entity join table

The per-transport registries don't define "devices" — they **record the originating-ability binding** that pins
a Node (by its transport address) to an Entity (`device_id` + `area`):

| Registry | Transport address (Node side) | Resolves to (Entity side) |
|---|---|---|
| `instance/devices.yaml` | BLE MAC / synthetic gas key | `device_id`, `area` (+ `node_id` for the gas split) |
| `instance/tasmota-devices.yaml` | Tasmota `%topic%` | `device_id`, `area` |
| `instance/levoit-devices.yaml` | ESPHome node name | `device_id` (area authoritative from `control.yaml`, ADR-0027) |
| `instance/control.yaml` | `device_id` (keyed on the Entity) | area + actuator traits (the Entity's actuate abilities) |
| `edge/*/nodes.yaml` | `node_id` ↔ eFuse MAC | the **Node** itself (ADR-0020 identity binding) |

`ha-edge-mapper` / the bridges perform the resolution; the **dictator owns it** (ADR-0001). The canonical output
of every path is one shape — `home/<area>/<device_id>/state` — which is why transport is just a tag, not a fork.

## Reasoning about a new (or grown) device

1. **Node** — what physical unit is it, and what's its transport / repoint class (`mqtt-broker` / `edge-signed`
   / `ble-passive` / `local-driver`)? That answers "how do I bring it up / move it".
2. **Abilities** — enumerate every function it performs; for each, run the CONFORMANCE.md **DO / CHECK / ADHERE**
   gate (a new ability on an existing node re-opens the checklist for that ability). Category and obligations
   come from here, not from a product name.
3. **Entity(ies)** — which `device_id`(s) does it surface, and in which `area`? Default: **one entity per
   physical appliance** (splitting is supported but not the default). Register the Node→Entity binding in the
   matching registry above.

See [`docs/DEVICE-INTAKE.md`](DEVICE-INTAKE.md) for the full intake flow; this section is the object-model lens
on it.

## See also

- [ADR-0034](adr/ADR-0034-device-object-model-node-ability-entity.md) — the decision record + resolved trade-offs.
- [`CONFORMANCE.md`](CONFORMANCE.md) — the ability catalog (A–K) and per-ability SHALL contracts (the by-contract axis).
- [`REUSE.md`](REUSE.md) — the by-capability catalog · [`AGENTS.md`](../AGENTS.md) tree — the by-location axis.
- ADR-0026/0027 (area taxonomy + actuator-area contract) · ADR-0015 (relay coverage) · ADR-0001 (dictator owns
  the registry) · ADR-0012 (transport-agnostic comms events).
