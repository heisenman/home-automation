# CONFORMANCE.md — what a device must adhere to, indexed by what it *does*

**The third navigation axis.** The repo already routes **by-location** (the [`AGENTS.md`](../AGENTS.md) tree,
ADR-0021) and **by-capability** (["what can I reuse"](REUSE.md), ADR-0025). This is the **by-contract** axis:
*"my device does X — what must it obey?"* ADRs are not just design records; the accepted ones are **normative
directives**, and a device is only a citizen of this system if it meets the contracts for the things it does.

## Why this is indexed by ability, not by device

A device is a **bundle of abilities**, and a capable one is a large bundle — the D1001 is simultaneously an
edge relay, a data-of-record replica, a display, a battery device, a BLE gateway, and OTA-updatable. Its
obligations are the **union of the requirements of every ability it exercises** — *derived*, never written
down per-device.

We index by **ability** on purpose:

- **A device list rots.** Enumerate conformance per-device and the day someone forgets to register a new
  device — or forgets that an existing device grew a new ability — that device silently escapes its contracts.
- **An ability list is looked up by the person building the ability.** An engineer adding "this panel now
  relays BLE" will look up *relays BLE* and find its requirements. Obligations follow **function, not name**,
  so a missed registration can never excuse a missed contract.

## How to use this — DO / CHECK / ADHERE

- **DO** — enumerate what the device *does*: every ability below that it will exercise.
- **CHECK** — for each ability, read the ADR(s) it **Binds**, and the **SHALL** items.
- **ADHERE** — implement every SHALL for every ability it exercises. **A device is not done until it conforms.**
  A new ability on an existing device re-opens this checklist for that ability.

Applies to firmware devices (edge nodes, panels), LAN actuators, and cluster boxes alike — each is a bundle of
the abilities below.

---

## Universal — every device, unconditionally

**Binds:** ADR-0001 (dictator authority), ADR-0015 (identity), repo posture.

- **SHALL** carry a **registered logical identity** (`device_id` / `node_id`) and be keyed by it everywhere.
  A raw MAC is only a BLE transport address, resolved from the registry **by the dictator** — never a key
  clients or nodes reason about (ADR-0001, ADR-0015).
- **SHALL** hold **no policy or authority**. All authority lives in the dictator (the single PEP); a device
  senses, relays, and acts on signed directives — it never decides (ADR-0001).
- **SHALL NOT** ever place a secret (WiFi password, per-device command secret, MAC, master passphrase,
  bearer token) into git, logs, or transcripts.

---

## The ability catalog

### A. Reports telemetry / senses
**Binds:** ADR-0001, ADR-0007, ADR-0012, ADR-0014 (R4).
- **SHALL** relay raw readings keyed by identity; the **dictator owns** the registry and the MAC→device/area
  mapping (`ha-edge-mapper`). The device never maps or interprets its own readings (ADR-0001).
- **SHALL** be safe to re-ingest: history is idempotent on `UNIQUE(device_id, ts, metric)` — a device may
  leave `ts` empty for the server to stamp, but must not assume its own writes are unique (ADR-0007).
- **SHALL** treat a device's **own onboard sensor as non-authoritative by default** (`authoritative=0`) —
  ingested for visibility, excluded from area rollups and from being a control source unless the user
  explicitly elects it (ADR-0014 R4).

### B. Exposes controllable traits (actuator)
**Binds:** ADR-0002, ADR-0014 (R1–R6), ADR-0010.
- **SHALL** describe its capabilities by the **trait vocabulary** (`switchable / ranged / positionable /
  lockable / setpoint`), not product names; policies target traits (ADR-0002).
- **SHALL** surface **every declared trait** as a user-facing control — no capability ships hidden (R1).
- **SHALL NOT** auto-wire a sensor→actuator binding; the user confirms which sensor(s) are valid sources
  (R2), and a device's own sensor is never an automatic source (R4).
- **SHALL** verify advertised capabilities **live** (reported-state readback + physical confirmation) before
  trusting the ranges; record verified ranges with a dated note (R3).
- **SHALL** admin-gate all actuation/override/policy edits and **verify the credential at unlock** — never
  store a credential that fails silently later (R5). Safety interlocks + cycle-protection have fixed
  precedence (R6). Acting on a command additionally requires the signed-directive path (ability C).

### C. Acts on signed directives (authority-bearing / enrolled signing node)
**Binds:** ADR-0010, ADR-0005. **Also requires:** ability **G** (time-synced) for the freshness check.
- **SHALL** verify, before acting, using **HMAC-SHA256 with its per-device secret**: the **signature**
  (constant-time), the **timestamp freshness** (default 30 s window), and — for sensitive actions — that the
  **nonce has not been replayed**. Anything failing is **refused** (ADR-0010).
- **SHALL** enforce **monotonic `(ts, seq)` anti-replay** persisted in NVS (`ha_cmd`): act only if strictly
  newer than the last acted-on. `ts` is server-stamped, so this holds regardless of node clock drift.
- **SHALL** hold a **per-device secret enrolled at the server console** (physical presence), stored in a
  gitignored secrets store; rotation is per-device (drop the secret to revoke).
- **SHALL** be an authority-bearing node under ADR-0005: Secure Boot + cable-only flashing — but eFuses are
  **never irreversibly locked** (USB recovery is always preserved). Pure sensor-relay nodes skip Secure Boot.

### D. Relays BLE adverts (edge relay node)
**Binds:** ADR-0015, ADR-0023, ADR-0001.
- **SHALL** obey the dictator's **per-node adv-filter directive** (which endpoints to relay), keyed by
  `device_id` resolved to MAC(s) by the dictator; the node stays dumb (ADR-0015, ADR-0001).
- **SHALL** emit a periodic **reach census** on `home/edge/<node>/reach` — a compact
  `[{mac, rssi_ewma, count, last_heard}]` of everything heard in the window, **independent of the relay
  allowlist** and RSSI-smoothed at the source (EWMA). This holds even for a `relay-none` node, so no node is
  ever invisible to rebalancing (ADR-0023).

### E. Renders a display / panel
**Binds:** ADR-0019, ADR-0013.
- **SHALL** be a **thin renderer of the shared BFF view-model** — consume the same LIVE surfaces the PWA
  reads (`/api/v1/{sensors,displays,alerts,house}` + MQTT `home/<area>/<id>/state`). **No panel-specific
  server endpoints** (ADR-0019, ADR-0013).
- **SHALL** keep the **UI app manifest-driven and updatable without a reflash** — the firmware host holds the
  renderer + fixed tile primitives; which rooms/tiles/bindings it shows is fetched, not compiled (ADR-0019).

### F. Battery-backed operation
**Binds:** ADR-0024.
- **SHALL** implement a **state-normalized gauge**: normalize the raw reading to the base frame (on-battery /
  display-on / not-charging) via measured per-state offsets **before** the V→SoC LUT; **race-safe ADC init**
  (serialize under a mutex, publish only on success); a **symmetric filter** (never latch SoC low on a load
  step).
- **SHALL** anchor conservatively: **0% = a safety floor above the electrical knee** (a shutdown trigger with
  ~5–8% reserve), **100% = the charger's own termination voltage** (never a guessed full-OCV number); and be
  onboarded via the **characterization procedure**.
- **SHALL** run the **four-part low-power policy** (run-floor / warn / warn-clear / boot-gate) that holds even
  before an accurate curve exists (reuse `ha_power_policy` / `ha_battery_profile`).

### G. Holds a wall clock (time-synced)
**Binds:** ADR-0010 (freshness). **Required by:** ability **C**.
- **SHALL** sync from the **LAN time authority** — the dictator/router holdover anchor; the **server is the
  time authority**. A device that acts on signed directives (C) **requires** this for the freshness window;
  a device that only displays a clock may sync best-effort and must **degrade gracefully** when unsynced.
- Coordination does **not** require a node clock: `ts` is server-stamped and monotonic by design, so a
  clockless device is fully coordination-correct (this is why panels may run clockless).

### H. Holds data-of-record / recovery / hot-standby
**Binds:** ADR-0018, ADR-0016, ADR-0007, ADR-0022, ADR-0019 (§4).
- **SHALL NOT** be trusted as archive-of-record until it passes the **record-keeping HARD GATE**
  (archive-parity vs the source — no shallower, no materially smaller). Control failover never waits on this;
  archival trust does (ADR-0018).
- **SHALL** reconcile **row-level** on the idempotency key `(device_id, ts, metric)` — a DISTINCT union, never
  a file rsync that must pick a loser (ADR-0018, ADR-0007).
- A **panel doubling as a recovery node** (ADR-0019 §4) SHALL keep a local rung replica (ADR-0022 ladder /
  `ha_replica`) with honest `source:` provenance, and mark its recovery eligibility in its `roles:` profile.

### I. OTA-updatable
**Binds:** ADR-0010, ADR-0005.
- **SHALL** treat OTA as a signed directive: verify the **signature over `url + sha256 + version`** (origin),
  then verify the **downloaded image's SHA-256** against the signed hash (integrity) before flashing, and
  **refuse a downgrade** (soft / NVS-based) (ADR-0010).
- **SHALL** keep OTA **rollback-safe and USB-recoverable** — never eFuse-lock the node (ADR-0005). (Rollback
  stops bricking, not malice — it is not a substitute for the signature checks.)

### J. Emits / consumes comms events
**Binds:** ADR-0012.
- **SHALL** speak the **normalized event vocabulary** — `reachable | unreachable | auth_expired | stale |
  degraded | acked | no_ack | refused` — as `CommsEvent(ts, device_id, transport, kind, detail)` on
  `home/_event/<device>`, so the controller (fail-safe), router (reroute), and UI (health) stay
  transport-agnostic (ADR-0012).

### K. Provisioned as a cluster box (can-ingest / VIP-eligible / record-keeping)
**Binds:** ADR-0018, ADR-0001.
- **SHALL** advance through the three explicit statuses — **can-ingest → VIP-eligible → record-keeping** —
  via the scripted, idempotent, **gated** provisioning procedure (`provision-peer.sh`); "ran for a while" is
  **not** record-keeping (ADR-0018). A box that holds the VIP but hasn't passed the archive gate is flagged
  by `cluster-doctor`.

---

## Deriving a device's obligations (worked examples)

A device's contract is the **union** of the abilities it exercises. Illustrative, not a maintained registry:

| Device | Abilities it exercises | Must adhere to |
|---|---|---|
| **D1001** (P4 panel) | A, C?†, D, E, F, H, I, J | Universal + A, D, E, F, H, I, J (†panel is *not* an enrolled signing node — it takes ops MQTT commands, not signed directives — so C/G do not bind; see [beachhead_main.c:568](../provisioning/reterminal/beachhead/main/beachhead_main.c#L568)) |
| **E1001** (S3 ePaper) | A, E, F, G(display-only), I, J | Universal + A, E, F, I, J + G best-effort (wall clock, degrades gracefully) |
| **ESP32-C6 relay** | A, C, D, G, I, J | Universal + A, C, D, G(required by C), I, J |
| **Dictator / standby box** | H, K (+ owns all authority) | Universal + H, K |

The `†` on D1001 ability C is the whole point of ability-indexing: the panel's obligations are read off *what
it does*, and "acts on signed directives" is deliberately **not** one of them — a fact currently buried in a
code comment that this catalog makes explicit and checkable.

## Enforcement — today and next

**Today:** this catalog is the authoritative, hand-curated source of truth; the "abilities" here are the
formalized capability vocabulary (they generalize ADR-0002's control traits and ADR-0019's `roles:` tags into
one binding vocabulary). Conformance is reasoned, not yet machine-checked.

**Next (follow-on, mirrors [REUSE.md](REUSE.md) / [edge/MATRIX.md](../edge/MATRIX.md)):** each device declares
its abilities in the registry (extend the ADR-0019 `roles:` / `capabilities:` fields to this enum); a generator
crosses **declared abilities × this catalog** into a per-device conformance matrix, and a drift-test fails when
a device declares an ability it hasn't met — turning "reasoned" into "enforced." That is a distinct task
(`device-conformance-requirements` follow-on), not a prerequisite: the catalog stands on its own now.

*See also:* [AGENTS.md](../AGENTS.md) Principles (the DO/CHECK/ADHERE gate) · [the ADR index](adr/) ·
[REUSE.md](REUSE.md) (capability reuse) · [ADR-0021](adr/ADR-0021-repo-documentation-tree-agent-navigation.md) /
[ADR-0025](adr/ADR-0025-reuse-first-navigation.md) (the other two navigation axes).
