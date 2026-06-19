# Offline-First Home Automation System — Architecture & Project Plan

Chat link: https://claude.ai/share/002dddf7-fb74-4662-b112-f841d4a001d9
**Status:** Living document — v1.0
**Last updated:** 2026-06-19
**Format:** Markdown, intended as the single portable reference across web chat, Claude Desktop, and VS Code / editor sessions, and as the basis for a public GitHub repository.

> **How to use this document.** This is the canonical design reference. Wherever development happens, this file (and the change records it mandates) are the source of truth. If a decision is made or changed anywhere — a chat, an editor, a commit — it must be reflected here or in the linked decision log (see §17). Code that contradicts this document is a bug in one of the two; reconcile, don't diverge.

---

## Table of Contents

1. [Goals & Design Principles](#1-goals--design-principles)
2. [System Overview](#2-system-overview)
3. [Hardware](#3-hardware)
4. [Device Taxonomy & Trust Classes](#4-device-taxonomy--trust-classes)
5. [Connectivity & Networking](#5-connectivity--networking)
6. [Data Ingestion](#6-data-ingestion)
7. [The Message Contract](#7-the-message-contract)
8. [Storage Architecture](#8-storage-architecture)
9. [Presentation Layer](#9-presentation-layer)
10. [Authority Model — The Dictator](#10-authority-model--the-dictator)
11. [Admin & Policy Layer](#11-admin--policy-layer)
12. [Operating Modes](#12-operating-modes)
13. [Security Architecture](#13-security-architecture)
14. [Backup & Restore](#14-backup--restore)
15. [Firmware & OTA](#15-firmware--ota)
16. [Self-Observability & Maintenance](#16-self-observability--maintenance)
17. [Project Governance & Working Practices](#17-project-governance--working-practices)
18. [Publication / Open-Source Guidance](#18-publication--open-source-guidance)
19. [Build Roadmap](#19-build-roadmap)
20. [Sign-Off Gate Checklist](#20-sign-off-gate-checklist)
21. [Glossary](#21-glossary)

---

## 1. Goals & Design Principles

**Purpose.** A house management system that stores, presents, and acts on data from environmental sensors and (later) controls actuators, built and owned end to end.

**Hard requirements**

- **Offline operation.** The system must function with no internet connection. Internet is acceptable only for one-time device provisioning/setup; never for runtime operation.
- **Hand-rolled comms & decision-making.** No vendor SDK lock-in. The internal nervous system is an MQTT bus with our own decoders and logic. Open libraries may be used as protocol references or sandboxed components, but the comms/decision infrastructure is ours.
- **Keep everything.** Full-resolution retention of all readings, indefinitely. Storage efficiency is solved with compression, not by discarding data.

**Design ethos**

- **Stability first.** Stability outranks features and outranks security hardening. The house is not a fort. Security that can lock the system out of itself is worse than the threat it prevents.
- **Passive-intrusion resistant, not active-attacker hardened.** Defend against passive sniffing and rogue/guest devices on the LAN. Do not gold-plate against determined active attackers or MITM; that's out of scope for a residence.
- **Evolve with the hardware.** Configuration evolves by data and policy, not by anticipating every future device. A capability contract lets new hardware be inducted without rewrites (§11).
- **Single authority.** One coordinator (the "dictator"); no peer consensus, no arguments (§10).
- **Trade CPU for storage and safety.** Compute is abundant relative to this workload; spend it on compression, verification, and sandboxing.

---

## 2. System Overview

Layered, hub-and-spoke. The server (dictator) is the single source of truth and the single mediation point. Everything else is a spoke.

```
        Humans (operators, residents)
              |   (UIs only; never touch devices directly)
        +-----v------------------------------------------+
        |   DICTATOR (primary server)  + warm standby    |
        |  - MQTT broker (Mosquitto)                     |
        |  - Ingest services (BLE scan / GATT poll)      |
        |  - Storage: SQLite (hot) + Parquet (cold)      |
        |  - DuckDB-backed API (summaries + deep dive)   |
        |  - Admin/Policy layer + device registry        |
        |  - Time authority (local NTP)                  |
        +--------------------|---------------------------+
                             |  MQTT bus (auth + ACL + local-CA TLS)
        +--------------------v---------------------------+
        |  Edge nodes (ESP32-C6) | Displays (D1001/E1001)|
        |  - BLE scan/relay      | - local SQLite render  |
        |  - sandboxed drivers   | - control surfaces     |
        +--------------------|---------------------------+
                             |  local BLE (no cloud)
        +--------------------v---------------------------+
        |  Sensors / actuators (SwitchBot, Aranet, SGP41)|
        |  (closed appliances are observed, not trusted) |
        +------------------------------------------------+
```

**Data plane:** sensor readings flow up via MQTT into storage. **Control plane:** commands flow down through the server, which arbitrates and confirms. **Provisioning plane:** devices are enrolled/registered at the server before doing anything.

---

## 3. Hardware

### 3.1 Server (the dictator)

- **Current box:** stand-in only (X570 / Ryzen 7 5800X media server, EXT4, enterprise SSDs, no RAID, 16 TB daily-spin backup platter in-chassis). **Out of project scope** — used for early development.
- **Target box:** low-power mini-PC class, 1–4 TB SSD (SATA or M.2), 12 V DC barrel-jack input preferred (runs directly off a 12 V battery). Possible internal redundancy; likely a flash drive as a local warm backup.
- **Power:** DC UPS / LiFePO4 sized for multi-day runtime at ~10–20 W. **Avoid power-station "UPS modes"** that auto-shut-off under a low-load threshold (Jackery/EcoFlow/Bluetti behavior). Prefer a true DC UPS feeding 12 V directly.

### 3.2 Failover server

- Second low-cost box, **truly redundant warm standby** (§10.3). Mirrors authoritative state continuously, issues nothing until it holds the floating VIP.

### 3.3 BLE / Wi-Fi adapter (dev bootstrap)

- **EDUP MT7921AU** USB combo (Wi-Fi 6 + BT 5.2). In-kernel `mt7921u` driver; **requires kernel ≥ 5.18, 6.6+ recommended** (current dev box must move off 5.15 to the 24.04 GA 6.8 kernel).
- Mount on a **USB 2.0 port or short extension cable** to avoid USB 3 2.4 GHz interference.
- The dongle is a **dev bootstrap**; ESP32 nodes are the real whole-house coverage fabric. Avoid Realtek Wi-Fi-6 USB combos (poor Linux drivers).

### 3.4 Edge nodes

- **Seeed XIAO ESP32-C6** — BLE + Wi-Fi 6 + 802.15.4 (Thread/Zigbee, future brand-agnostic headroom). Cheap; scatter for coverage; host sensors/actuators.
- Per-node **mini DC-UPS** (LiFePO4, 5 V/12 V output).
- **Keep spares** of each node type for brick recovery; keep one **bench canary** continuously attached to the server for firmware validation (§15.5).

### 3.5 Displays

- **reTerminal D1001** (ESP32-P4 + C6 coprocessor, 8" color touch LCD): rich interactive panel, comms node, and on-device dashboards with local DB rendering. Mains-powered.
- **reTerminal E1001** (ESP32-S3 e-paper, deep-sleep, ~3-month battery): glanceable dashboard and **discrete control triggers** (button → command → confirm → sleep). **Not** for interactive control or continuous sensing (its battery model fights both).
- **Future:** tablet-class clients (thin clients of the server API).

### 3.6 Sensors

- **SwitchBot Meter Pro + outdoor meters** (~15): passive BLE advertisers (manufacturer ID `0x0969`); read-only.
- **Aranet Radon Plus (TDSPSRH2)**: radon + temp + humidity, locally BLE-readable (`aranet4` lib as protocol reference). Enable "Smart Home Integration" broadcast to read it passively like the meters. Lives in the crawlspace near the server (a place otherwise hard to monitor).
- **Grove SGP41** (VOC/NOx): I2C sensor, **no firmware of its own** — attaches to a host MCU (C6 or D1001). Continuous-operation MOX sensor; needs RH/T compensation fed from a nearby meter. Belongs on an always-powered node, **not** the deep-sleep E1001.
- **Airthings View Radon (2989)**: Wi-Fi/cloud device, **not locally BLE-readable**. Walled off from the offline core; optional internet-dependent cross-check only. The Aranet is the authoritative local radon source.

---

## 4. Device Taxonomy & Trust Classes

Three classes, and "flashable" matters for only one:

1. **Programmable nodes (you flash):** XIAO C6, D1001, E1001. Your firmware lives here. On the D1001 you flash the P4; its C6 is a radio coprocessor you build against.
2. **Attached peripherals (driven, not flashed):** Grove SGP41, the USB dongle. No firmware of their own; their host's firmware drives them.
3. **Closed appliances (observed, never trusted):** SwitchBot, Aranet, Airthings. Integrated by consuming their output, not by changing them. Permanently behind the untrusted-ingress boundary (§13.4).

**Authority sub-classification** (drives hardening, §15.6): a node has *authority* if it can drive an actuator (especially a lock). No-authority nodes (sensor relays) get lighter firmware hardening; authority-bearing nodes get the strong version.

---

## 5. Connectivity & Networking

- **All sensor reads are local BLE advertisement scans.** Actuator commands are local BLE GATT writes. No SwitchBot cloud, no webhooks.
- **MQTT bus (Mosquitto)** is the internal nervous system; one broker, on the dictator (§10).
- **Wi-Fi network selectivity (NetworkManager):**
  - Wired primary, Wi-Fi failover (Ethernet route metric below Wi-Fi; set explicitly).
  - Pin the intended SSID; disable auto-connect to any other/open network.
  - Keep **broker address and bind interface in config**, not hardcoded, so migration to the future dedicated IoT network + new hub is a config change.
- **Future:** dedicated isolated IoT VLAN/SSID (no WAN route); local DNS/discovery (no internet DNS); possible MQTT bridging across segments. The dongle does BLE on `hci0` (BlueZ) and Wi-Fi on `wlan` (NetworkManager) — same chip, separate stacks; Wi-Fi is an idle backup so coexistence is a non-issue.

---

## 6. Data Ingestion

Three lanes, one bus:

1. **Passive BLE scanner** (Python + `bleak`): the heart. Listens to all advertisements, decodes by manufacturer/service ID (SwitchBot, broadcast-mode Aranet), publishes normalized readings to MQTT. No connection, no rate limit, continuous. **Build this first.**
2. **GATT poller:** scheduled connect → read → disconnect for non-broadcasters (slow-update sensors). Has a slot in the architecture even if initially unused.
3. **Walled cloud lane:** for internet-dependent sources (Airthings). Clearly marked, never wired into the offline core.

- **Deduplication / multi-listener merge:** with overlapping listeners (server dongle + ESP32 nodes), the same advertisement arrives by multiple paths. The server is the single sink → dedup by `device_id` + sequence/timestamp; merge rule keeps best-RSSI or first-seen.
- **Protocol references:** `pySwitchbot`, `aranet4`, `airthings-ble`. Use as decoders/references; the comms/decision logic remains ours.

---

## 7. The Message Contract

The data-plane spec everything hangs off. Publish one **retained** JSON state message per device.

**Topic:** `home/<area>/<device_id>/state` (retained → new subscribers instantly get last value)

**Command topics** are separate from state topics: `home/<area>/<device_id>/cmd` and `.../cmd/ack`.

**Payload:**

```json
{
  "schema": 1,
  "device_id": "switchbot_meter_kitchen",
  "device_type": "switchbot_meterpro",
  "area": "kitchen",
  "ts": "2026-06-19T14:32:05Z",
  "transport": "ble-adv",
  "metrics": { "temperature_c": 21.4, "humidity_pct": 48, "battery_pct": 92 },
  "meta": { "rssi": -67, "mac": "..." }
}
```

- **SI units in metric names** (no ambiguity). **`schema` version field** so old Parquet remains readable as the contract evolves.
- **`transport`** distinguishes `ble-adv` / `ble-gatt` / `cloud`.
- **`area`** carried from the device registry (§11).
- **Commands** carry an ID, a server-issued **freshness nonce** for sensitive actions (replay resistance), and resolve via a `cmd/ack` result. Closed-loop model: **intended state vs reported actual state vs source-of-change** (commanded / autonomous / manual) — see §13.6.

---

## 8. Storage Architecture

Two tiers + a derived summary tier, one writer service.

- **Hot tier — single SQLite file.** Writer subscribes to `home/+/+/state`, appends each reading. One durable, queryable file; no small-file amplification. Holds ~current day/week.
- **Cold tier — partitioned Parquet (Zstd).** On a daily/monthly cadence the writer compacts hot rows into one Parquet file per partition (Hive-style `year=/month=/`), then prunes them from SQLite. **Long format:** `ts, device_id, metric, value, unit, area, transport`. Columnar + Zstd → ~10–30× compression; years of data in hundreds of MB.
- **Summary tier — computed in the same compaction pass.** Per-device/metric aggregates (min/max/mean/median/count/last, optional stddev/percentiles) over standard windows. One read, two outputs (raw partition + summary rows). Feeds dashboards and display clients.
- **Query:** **DuckDB** — embedded, no server. Queries the hot SQLite and the Parquet glob in a single SQL statement.
- **EXT4 caveat:** EXT4 has **no data-block checksums**. Bit-rot defense moves to the application layer: maintain a **hash manifest** of immutable Parquet partitions (plus Parquet's internal page CRCs) and **verify on a schedule and before any restore**. This replaces the ZFS/btrfs scrub you don't have.

**Anti-pattern to avoid:** writing a Parquet file per reading/minute (small-file/inode blowup). Batch via the SQLite hot tier; flush in large partitions. Flush cadence (daily vs monthly) is the one tuning knob — both keep files large.

---

## 9. Presentation Layer

Custom, DuckDB-backed; off-the-shelf (Grafana) only for dev-time engineering visibility.

- **API service** (FastAPI or similar) on the dictator, single owner of the data. Endpoints:
  - **Summary (default):** precomputed summary tier + **live "today" computed on the fly from the hot SQLite** (so current values aren't stale until next compaction).
  - **Deep dive (explicit):** DuckDB over the full Parquet archive. **Bounded/queued** to prevent accidental or malicious query-DoS against the single server.
- **Clients** are thin and call the same JSON API: custom web app; D1001 (interactive); E1001 (glance); future tablets.
- **On-device rendering (displays):** the local DB on a display is **SQLite, not Parquet/DuckDB** (DuckDB/Parquet don't run on MCUs; SQLite does, reading from microSD, indexed lookups sub-second even on millions of rows). The server pushes a **right-sized, read-only SQLite snapshot** (summary tier + a bounded recent-raw window) on the compaction schedule, written atomically (temp + rename). Firmware queries it via embedded SQLite + LVGL. For full-archive deep dives, firmware falls back to the server API. Read-only on the device → no flash wear, no locking.

---

## 10. Authority Model — The Dictator

### 10.1 Principle

Centralized **star authority**: single source of truth, single **Policy Enforcement Point (PEP)**. No peer consensus, no leader election, no multi-master reconciliation. Collapsing the interface mesh into a star is *also* the security model (one hub to secure). **Humans never touch devices directly**; all human intent routes through the server. **No two non-server entities form trust the server didn't broker.**

### 10.2 Time

Server is the **time authority** (local NTP; plug-in RTC or GPS-disciplined source if the motherboard clock drifts). Displays' RTCs (D1001 PCF8563, E-series RTC) are **subordinate watchdogs** — they compare and alert on divergence but never correct. For passive advertisements, stamp centrally on receipt; for edge-originated data, rely on synced clocks.

### 10.3 Failover

- **Floating service address (VIP) via keepalived/VRRP.** All devices target the VIP, never a specific box. VRRP guarantees exactly one holder; on primary loss the VIP migrates to the standby. Devices see only a brief blip.
- **State mirroring by type:** immutable Parquet → incremental rsync; live hot SQLite → continuous WAL streaming (e.g., Litestream) or hand-rolled WAL shipping; config/registry → git/file sync; broker retained state → broker bridging or subscribe-and-persist; secrets → at rest on both, provisioned once.
- **Standby is warm-but-mute:** services up, state flowing in, issues nothing until it holds the VIP.
- **Fencing (the one real subtlety):** a dictatorship's only failure is **two pretenders** (split-brain). Promotion must guarantee single-authority-at-a-time. On a single LAN, VRRP suffices; add a simple tiebreaker (refuse to promote unless the gateway is also reachable) if desired. Don't over-build.
- **RPO:** **asynchronous** replication (seconds of potential loss, better write performance/stability) — the right call for a house. A documented sign-off item.

---

## 11. Admin & Policy Layer

### 11.1 Capability contract (the keystone)

Behavior is configured against a **small trait vocabulary**, not products: `switchable`, `ranged` (0–100), `positionable`, `lockable`, `setpoint`. A device is a mix of traits (a dimmer = switchable + ranged; a curtain = positionable). Policies target traits ("on server-down, set every `lockable` to locked") so they cover all current and future devices of that shape with no new code. This is the proven pattern (Matter clusters / HomeKit characteristics / HA domains / Z-Wave command classes).

- The vocabulary is **small and grows ad hoc** — no ecosystem-grade ontology. Extending it is cheap.
- An **extras hatch** carries device-specific, fine-grained, low-power features the generic contract flattens.
- Trait assignment happens at **server-mediated onboarding** (optional node hinting; the dictator confirms).

### 11.2 Implementation spectrum (generic ↔ bespoke)

The capability contract is a **thin uniform interface**, not a labor-saving compromise. Behind it, implementations slide from **generic-auto** (instant induction; a new device works at basic level day one) to **fully bespoke driver** (lowest-power, finest-grained, vendor-special features). A bespoke driver still **presents the standard capabilities upward**, so the admin layer is unchanged.

- **Discipline:** bespoke freedom **below** the contract line, uniform interface **above** it. The instant device specifics leak upward into admin/dictator logic, the coupling-containment benefit is lost. *This is the durable reason to keep the abstraction even though code is now cheap to generate.*
- **Decision rule:** ride the generic handler where adequate; spend a bespoke driver only where the optimization payoff justifies the added **verification** surface (generation got cheap; *trust* did not, especially for actuators).

### 11.3 Policy categories (configured per device/capability)

Power-on/join default · disconnected fallback ("standing orders," pushed to nodes) · safe/fail-safe state · guardrails (min/max, rate limits, allowed hours, interlocks) · command authorization (PIN/confirm on sensitive actions) · whole-house modes (home/away/night/emergency).

### 11.4 Admin layer mechanics

UI/API on the dictator editing a **versioned, validated** policy store (authoritative). Every change **validates against the capability schema** before activation; dry-run/preview and **staged rollout** (one actuator → confirm → broaden) given physical stakes; **versioning, audit, rollback**. Validated policy is **pushed to each owning node** so it can act autonomously when disconnected (the standing orders fall out of this).

### 11.5 Policy vs automations

Keep **declarative default behavior/policy** (state + guardrails) distinct from **reactive automations** (condition → action). Both read the same capability model; fusing them early creates a tangle.

---

## 12. Operating Modes

A first-class, **server-owned global mode** — a small enum, e.g., `Normal` / `Conserve` / `Emergency` — acting as a multiplier on every device's capability policy.

- **Normal (wall power):** aggressive scanning, TLS everywhere, frequent display refresh, server runs heavy analytics.
- **Conserve (battery/grid-down):** reduced duty cycles, slower telemetry, no interactive refresh, deferred compaction, possibly TLS scoped to command/lock topics only.
- Several earlier perf-vs-security tradeoffs become **mode-driven** rather than static.

**Requirements:** mode is **server-declared and authenticated** (a spoofed "enter Conserve" is a DoS/mischief vector); **hysteresis** to prevent thrashing near a threshold; mode-detection inputs are a **pluggable future dependency** (mains-present, UPS state, whole-house power monitor) — concept built now. Composes with server-down fallback: server unreachable **and** power low → device's pre-provisioned conservation standing orders run autonomously.

---

## 13. Security Architecture

Security is **mediation at trust boundaries**. The star topology yields a small, enumerable boundary set with the server as the single PEP.

### 13.1 Human → Admin (authoring)

Apex authentication. **Split privileges:** everyday **policy authoring** over the authenticated web admin; **trust-minting (enrollment/registration)** gated behind **physical presence at the server console** — there is **no wireless onboarding path** to create trust. All writes validated against the schema; versioned/audited; staged rollout before policy touches physical devices. Fails safe (loss of admin = frozen config, not loss of operation).

### 13.2 Human → Server → Device (view & control)

No direct Human→Device edge. Clients send read requests and command **requests**; the **server arbitrates and authorizes per action** (PEP), with sensitive actions (unlock) gated by an additional server-enforced factor (PIN/confirm). A compromised display can only *request*, never *act*. Fails closed on control, degraded-readable on view (cached SQLite + pre-provisioned triggers).

### 13.3 Server → Device (control plane & provisioning)

The boundary where a device **becomes trusted**. Server-rooted identity via a **local CA**; **per-device credentials**; enrollment over a trusted channel (USB / console); **signed, versioned policy pushes**; sensitive commands carry **freshness nonces**. Fails safe: server unreachable → device runs last-pushed policy autonomously, refuses unauthenticated control.

### 13.4 Any layer ↔ Network (transport)

Broker **authentication + topic ACLs** (a sensor node publishes only its own topics; cannot touch a lock command topic); **local-CA TLS** (the concrete defense against passive sniffing); **VLAN isolation**. Self-healing on broker loss (VIP/standby, buffering, retained-state re-sync).

### 13.5 Untrusted ingress (BLE read path + walled cloud)

**Zero trust.** A BLE advertisement cannot be authenticated — anyone can broadcast a spoofed one. Treat ingested sensor data as untrusted input: validate ranges/plausibility, rate-limit. **Hard gate:** sensor input must always pass through **validated server policy** before it can influence an actuator — never a raw sensor → actuator passthrough. The Airthings cloud path is firewalled from the offline core.

### 13.6 Physical access & actuators

"Physical access = no security." Anyone in the house can manually override an actuator. Therefore **an actuator is a monitoring surface, not a guaranteed control surface.** State model carries **intended state, reported actual state, and source-of-change** (commanded / autonomous / manual). Unexplained divergence (a lock reporting "unlocked" when policy said "locked") is a **first-class monitored event**, not a control failure to suppress.

### 13.7 Corroboration for high-consequence events

Because we own most of the communication, **high-consequence or aberrant events require independent corroboration** before action: a second nearby node must confirm an extreme sensor reading, or an actuator claiming aberrant behavior. **Key insight:** a crypto handshake proves *identity/integrity*; an **independent observer** proves *reality* — for spoofed **content**, corroboration is the defense, not signatures. **Tiered** to consequential events only (latency cost lands where stakes justify it). **Degrades safely:** no corroborator in range → flag suspect, **hold, and alert** — do not act on unconfirmed extreme data. The "extreme/aberrant" threshold is a tuned admin policy.

### 13.8 Threat model & posture

Passive-resistant, stability-first, not a fort. **Use:** local-CA TLS (passive-sniff defense), per-device creds + ACLs (rogue/guest device), replay nonces on lock commands. **Skip:** mutual-auth everywhere, IDS, cert-pinning gymnastics. **Compensating control for skipped Flash Encryption:** per-device credentials are physically extractable on a compromised node, mitigated by **central revocation + re-enrollment** (per-device scope limits blast radius). **Meta-principle:** auth must **degrade gracefully** — fail open on reads where sensible, keep a local break-glass path, never let a silent cert expiry take down the dictator.

### 13.9 Secrets

CA **private signing key offline** (USB/drawer; needed only to enroll a device). CA **public cert** distributed (not secret). Per-device creds issued once at enrollment, held at rest. **Secrets backups encrypted.**

---

## 14. Backup & Restore

**Replication is not backup.** Replication (standby + display snapshots) provides availability/distribution and faithfully propagates corruption, operator error, and malicious deletion. Backup is the **point-in-time, immutable, offline** thing that protects against those.

**Protect, by reconstructability (hardest first):**

1. **Config / policy / registry** — encodes human decisions; keep in **git** (history = backup + audit).
2. **Secrets** — encrypted backup; CA private key already offline.
3. **Parquet archive** — immutable, append-only; back up new partitions only.
4. **Hot SQLite** — consistent **point-in-time snapshot** (SQLite online-backup / `VACUUM INTO`), never a live-file copy.
5. **Summary tier** — regenerable; optional.
6. **Golden firmware images + server rebuild (IaC)** — also the brick-recovery source (§15.7).

**Mechanism & policy**

- **3-2-1 adapted offline:** ≥1 copy **air-gapped and off-site**. Target: a **backup box in a separate structure**, configured as a **periodic-pull** target (not a live replica) so corruption doesn't follow the data across. Covers fire/theft of the main structure; property-wide catastrophe is out of scope.
- **Bit-rot:** application-level **hash manifest + scheduled verification** (EXT4 has no scrub); multiple copies.
- **Cold-media retention:** unpowered consumer flash leaks charge over a year or two — don't trust it for multi-year cold. **Periodically power-and-verify/refresh**, or use periodically-spun HDD / write-once optical.
- **Tested restore drills** (the most-skipped, highest-value control): periodically rebuild from backup alone onto a scratch box/standby and verify integrity. **Prompted via the display nag system** (§16).

**Restore runbooks:** corruption → roll back to pre-corruption snapshot; operator error → git revert / point-in-time; total loss → OS rebuild (IaC) + git config + archive disk + encrypted secrets + offline CA key + re-flashed nodes.

---

## 15. Firmware & OTA

Scope: **our trusted nodes only**; dictator-mediated; internet-free.

### 15.1 Firmware tiering (Wasm)

- **Foundational firmware** — plain C, **cable-flashed (hardline-preferred)**, trusted, rarely changed: boot, radio, MQTT, identity/credentials, capability-contract handling. Embeds a small **WebAssembly runtime (WAMR**, an official ESP-IDF component, ~85 KB interpreter / ~50 KB AOT; **wasm3** as a lighter fallback for the smallest nodes).
- The foundation **exports a capability-scoped native host API** (e.g., "read this I2C register," "publish on my topic" — and nothing else).
- **Peripheral drivers** (SGP41, future sensors, bespoke actuator logic) compile to **sandboxed Wasm modules**, OTA-loaded (WAMR app-manager). The **sandbox enforces "no authority" at runtime** — a module can only call exported host functions and cannot reach credentials, the lock path, or the trust layer. A bad/malicious module misbehaves only within its sandbox (and corroboration catches bad readings); you OTA a fix.
- **Benefit:** shrinks the firmware verification surface — the **host API** is the one thing verified carefully; modules are cheap to trust because their blast radius is bounded.
- **Tradeoffs:** native C is faster/more energy-efficient (benchmarked on the C6); Wasm is an acceptable overhead for non-hot-path peripheral logic, with energy cost to watch on battery nodes doing frequent work. RAM (64 KB Wasm pages) is the real constraint on the bare C6 — size module memory tightly; comfortable on the P4 displays. Most ambitious single piece of the build — one-time platform investment.

### 15.2 Safety: A/B + rollback

**Non-negotiable for any actuator node.** Flash the inactive partition, boot it, run a **post-update health check** (reach broker / pass self-test), then mark valid — else **auto-rollback** to the known-good partition. The active partition is untouched until verified, so a power loss mid-update (real on battery/UPS nodes) cannot brick.

### 15.3 Security

OTA is the highest-consequence push: **signed images** (node verifies before install), **dictator-only delivery** over the authenticated channel, **console-gated signing/release** (physical presence to release a build), **protected signing key**.

### 15.4 Staged rollout

Never whole-fleet at once (especially actuators). Two-stage canary (§15.5) → waves. No OTA during Conserve/Emergency; deep-sleep nodes update on their wake window. Registry tracks per-node firmware version and detects drift.

### 15.5 Canary validation

- **Bench spare** continuously attached to the server: catches gross failures instantly on real silicon. Limit: can't validate peripheral-specific drivers without the peripheral attached, or the in-situ RF/power environment.
- **In-situ node:** second canary with real peripherals and surroundings before fleet rollout. Attach representative peripherals to the bench rig for peripheral-specific driver paths.

### 15.6 Secure Boot — authority-tiered

Hardening follows authority. **Signed images secure the update channel; Secure Boot secures against physical reflash** — different protections.

- **No-authority nodes (sensor relays):** **skip Secure Boot.** Signed OTA + per-device cred + central revocation is the proportionate control; keep cheap spares for bricking. The Wasm sandbox means a bad peripheral module can't escalate anyway.
- **Authority-bearing nodes (lock controllers, etc.):** **Secure Boot + cable-only flashing + spares.**
- **Do not irreversibly lock eFuses** on any node you want to keep USB-recoverable. Maximum Secure Boot + Flash Encryption with burned eFuses can permanently disable easy USB re-flash — keep image verification and rollback **without** irreversible eFuse locking, preserving recoverability.

### 15.7 Brick recovery

**USB re-flash** (preferred; JTAG avoided). ESP32 ROM serial bootloader over USB (`esptool`) is the recovery path. **Golden images live in the backup set** — firmware backup *is* the brick-recovery source.

---

## 16. Self-Observability & Maintenance

- **Liveness:** track last-seen per device; a sensor that stops broadcasting raises an alert, doesn't silently vanish.
- **Battery monitoring:** surface low batteries before they die.
- **Service supervision:** systemd with restart-on-failure + watchdog; a crashed scanner must not leave a silent gap.
- **Maintenance scheduler (first-class component):** tracks due human-in-the-loop tasks — **restore drills, off-site rotation, archive verification, battery checks, spare-node revalidation** — and surfaces them through the displays with **acknowledgment tracking and escalation** (badge → D1001 full-screen takeover → phone message), re-arming until the task is genuinely done. *A nag you can dismiss without doing the thing is theater; a nag that won't clear until the backup test passes is a control.* Nag aggressively.

---

## 17. Project Governance & Working Practices

> **Directive — Documentation is part of "done."** All project work, wherever performed (web chat, Claude Desktop, VS Code, editor), produces a documentation delta. No change is complete until this plan and/or the decision log reflect it.

> **Directive — Centralized documentation maintenance.** The git repository is the single source of truth. On any update or change, the operator is **prompted to centralize** the documentation (commit the plan/ADR/changelog deltas), regardless of where the work happened. Cross-surface work must converge in the repo; code or notes that live only in a chat are not yet real.

### 17.1 Documentation discipline

- **Single source of truth:** a git repo holds this plan, a `CHANGELOG.md`, and **Architecture Decision Records (ADRs)** — one dated, numbered record per significant decision (this conversation's decisions are the seed set: dictator model, capability contract, Wasm firmware split, EXT4 integrity strategy, authority-tiered Secure Boot, etc.).
- **Definition of done includes docs:** every PR/commit that changes behavior updates the relevant doc/ADR/changelog.
- **Decision log captures the *why*,** not just the *what* — rationale and rejected alternatives, so future-you (and contributors) don't relitigate settled tradeoffs.

### 17.2 Code coherence

- **Monorepo** (suggested layout): `server/` (ingest, storage, API, admin), `firmware/` (foundational + Wasm peripheral modules), `config-examples/`, `docs/` (this plan + ADRs), `tools/`.
- **Reproducible environments:** pinned dependencies (lockfiles), containers (Podman/Docker) or documented IaC for server services so a rebuild (restore / new server) is deterministic.
- **Firmware versioning** tied to the device registry (§15.4).
- **Config-as-code:** policies, registry, and capability definitions are versioned, validated, and live in the repo (instance values excluded — §18).
- **Lightweight CI** validates schema/config/firmware-version coherence and runs secret scanning (§18).

### 17.3 Data coherence

- **The message contract is the data contract** (§7); **schema-versioned** so old Parquet stays readable as it evolves.
- **Single device source of truth:** the registry (identity, capability classification, area, calibration offsets, MAC↔name).
- **Provenance:** every reading carries `source`/`transport`/`device_id`; the server is the single sink (dedup, timestamp authority).
- **Code/data interface:** the capability contract and message contract are the shared boundary; changes to either are ADR-tracked.

### 17.4 Testing strategy

- **Server:** unit + integration tests for ingest, storage/compaction, API; contract/schema tests.
- **Firmware:** the two-stage canary (§15.5); sandbox-bounded modules reduce per-driver risk.
- **Restore:** scheduled, *verified* restore drills (§14) — a backup is untested until restored.

### 17.5 Dependency & supply-chain notes

- Reverse-engineered protocols (`pySwitchbot`, `aranet4`) can break on vendor firmware updates — pin, test, and treat as a maintenance item; the offline-first design means you control *when* you update.
- Track upstream licenses for any redistributed component (§18).

---

## 18. Publication / Open-Source Guidance

The **architecture and code are publishable**; the **populated instance is not**. Keep a clean split.

### 18.1 Public vs private

- **Public repo:** code, architecture docs (this plan, genericized as desired), **example/template configs**, BOM, setup instructions.
- **Private (separate repo or git-ignored, local only):** real **device registry** (MACs reveal your specific devices and aid a near-house targeted attacker), **policies/standing-orders** (a map of your automation and locks), **secrets** (CA keys, broker creds, per-device creds), network topology, schedules.

### 18.2 Secrets hygiene (the "be careful which config files get uploaded" concern)

- **`.gitignore`** all instance config and secrets by default; commit only `*.example` / template files.
- **Pre-commit secret scanning** (e.g., gitleaks / trufflehog) as a hard gate, plus a CI scan.
- Principle: the repo contains the **system**, never the **instance**.

### 18.3 Licensing (your decision — not legal advice)

- Choose intentionally: **permissive** (MIT / Apache-2.0) for maximum reuse, or **copyleft** (GPL) to require derivatives stay open. Apache-2.0 also grants an explicit patent license.
- Check **dependency license compatibility** if you redistribute (e.g., WAMR is Apache-2.0-with-LLVM-exception; Mosquitto, DuckDB, and the protocol libs each carry their own terms). I'm not a lawyer — confirm specifics before release.

### 18.4 Liability / safety disclaimer

A system that controls physical actuators and locks, shared for others to run, should carry a clear **no-warranty / use-at-your-own-risk** disclaimer, with explicit notice around locks, safety, and security. Standard for open hardware/software, but warranted given the physical stakes.

### 18.5 Reproducibility for others

Publishing for others raises the bar on setup docs, the hardware BOM, and example configs — which the documentation discipline (§17) already produces.

---

## 19. Build Roadmap

Phased so the system is useful early and de-risked at each step.

1. **Ingest-and-store core.** Kernel 6.8 on the dev box; Mosquitto; the passive BLE scanner publishing SwitchBot (+ broadcast-mode Aranet) into the message contract; hot SQLite writer. Verify end-to-end with `mosquitto_sub`. *(Can start on the server's onboard/USB BLE before nodes arrive.)*
2. **Storage maturity.** Compaction to Parquet + summary tier; hash-manifest verification; DuckDB query path.
3. **Presentation.** DuckDB-backed API (summary default + bounded deep dive); first custom dashboard; Grafana on hot SQLite for dev visibility.
4. **Edge fabric & sensors.** ESP32-C6 nodes for coverage; SGP41 on an always-powered host with compensation; dedup/merge.
5. **Admin/policy layer.** Capability contract + registry; generic handlers; policy store with validation/versioning; standing-orders push.
6. **Control plane & actuators.** Command topics, closed-loop confirmation, freshness nonces; first actuators; corroboration tier; monitor-not-control state model.
7. **Resilience.** Warm standby + VIP failover; backup (incl. off-site box) and tested-restore drills; maintenance scheduler/nag.
8. **Displays & firmware tiering.** D1001/E1001 clients; on-device SQLite snapshots; foundational firmware + Wasm peripheral modules; A/B OTA; authority-tiered Secure Boot.
9. **Operating modes & power sensing.** Mode dimension wired into policy; future power/energy sensors to drive it.

---

## 20. Sign-Off Gate Checklist

Hard gates (a design that violates these does not pass):

- [ ] Sensor input **always** passes validated server policy before influencing an actuator (no raw sensor→actuator).
- [ ] Failover guarantees **exactly one authority** at a time (fencing).
- [ ] A/B partitions + auto-rollback + post-update health check on any actuator-touching node.
- [ ] Signed, dictator-only firmware; console-gated signing/release; protected signing key.
- [ ] No wireless trust-minting (console-only onboarding).
- [ ] Secrets never committed (gitignore + pre-commit secret scan); instance config kept out of the public repo.

Decision gates (deliberate, recorded choices):

- [ ] TLS scoping on constrained nodes (everywhere vs command/lock-only + isolated VLAN).
- [ ] Bounded/queued deep-dive queries (anti query-DoS).
- [ ] Command-latency expectations by device power-state (deep-sleep nodes are trigger-out, not fast command-in targets).
- [ ] Replication RPO = **async** (seconds) accepted.
- [ ] Corroboration thresholds for "extreme/aberrant" events tuned.
- [ ] Operating-mode hysteresis + authenticated mode changes.
- [ ] ≥1 offline/air-gapped **periodic-pull** backup; encrypted secrets backups.
- [ ] EXT4 bit-rot strategy: app-level hash manifest + scheduled verify; cold-media refresh cadence.
- [ ] Secure Boot authority-tiered; **eFuses not irreversibly locked** on USB-recoverable nodes.
- [ ] Wasm runtime footprint validated on the bare C6 (wasm3 fallback if needed).
- [ ] License chosen; dependency licenses checked; liability disclaimer added (if publishing).

---

## 21. Glossary

- **Dictator** — the single authoritative server (primary, with a warm standby).
- **PEP** — Policy Enforcement Point; the single place authorization/validation happens (the server).
- **Capability / trait** — a behavior primitive (switchable, ranged, positionable, lockable, setpoint) used as the uniform device contract.
- **Standing orders** — pre-provisioned local fallback policy a node runs autonomously when the dictator is unreachable.
- **Corroboration** — independent second-observer confirmation required before acting on high-consequence/aberrant data.
- **Foundational firmware** — the cable-flashed, trusted C base hosting the Wasm runtime and exported host API.
- **Peripheral module** — a sandboxed, OTA-loaded Wasm binary implementing a device driver with no system authority.
- **Hot / cold / summary tiers** — live SQLite / immutable Parquet archive / precomputed aggregates.
- **VIP** — virtual IP; the floating service address that defines "who is the dictator right now."

---

*End of plan. This is a living document: update it (and the decision log) with every change, wherever the work is performed.*
