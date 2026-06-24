# ADR-0011 — Modular Infrastructure & Failover-Ready Control Plane

**Date:** 2026-06-21
**Status:** Accepted — **LIVE** (reconciled 2026-06-24). Control plane built to this shape; **the G11
(`ha-dev` / `192.168.0.210`) is the live dictator**, and **keepalived/VRRP warm-standby failover is
implemented + tested** (`.245` standby, VIP `192.168.0.200`, primary-supremacy auto-demote — `failover/`).
VRRP-failover + G11 were "Phase 7 hardware" at authoring; both shipped 2026-06-24.

## Decision

The system is composed of four clearly-bounded tiers that interact only through narrow, authenticated
interfaces, so any tier can be replaced, scaled, or failed-over without touching the others:

- **Dictator** — the single authority/PEP (ADR-0001). Owns the registry, policy, secrets, time, and
  the only path that issues device commands.
- **Failover** — a warm-but-mute standby dictator. Identical software; gains authority only by holding
  the VRRP VIP (exactly-one-authority). Becomes the dictator by syncing a handful of files + the DB.
- **Edge nodes** — dumb, replaceable BLE/Wi-Fi relays + GATT proxies (ESP32-C6). Hold no policy; they
  scan/relay, and execute server-composed, **signed** directives. Adding/removing one needs no server
  code (the mapper resolves by registry).
- **Endpoints** — displays / control surfaces (D1001/E1001, future). **Request-only**: they ask the
  server; they never touch devices. Fail closed on control, degraded-readable on view.

**Interfaces between tiers are the contract, not the implementations:**
1. **Data plane** — MQTT telemetry up (`home/edge/<node>/<mac>/adv` → canonical `home/<area>/<dev>/state`).
2. **Control plane** — signed directives down (`home/<area>/<dev>/cmd`) + acks up (`…/cmd/ack`),
   every directive HMAC-signed per-device (ADR-0010). Authenticity is **software/cryptographic**, not
   physical — endpoints without buttons are first-class.
3. **Config plane** — the dictator's authority is a small set of **plain files**
   (`instance/control.yaml`, `instance/control_secrets.yaml`, policy, `devices.yaml`) plus the DB.

## Context

Guiding philosophy (Hugh, 2026-06-21): **security over the air** + **flexible modular infrastructure**
between dictator, failover, edge nodes, and endpoints. Hardware arrives incrementally (G11 ~2026-06-23;
more edge nodes in weeks), so the architecture must absorb new/replacement devices and a second server
without rewrites, and survive a dictator outage.

## Consequences

- **Failover is a file sync + a VIP, not a rebuild.** Control authority lives in plain files → the
  standby gets it by replicating them (rsync/sneakernet) and holding the VIP. The PEP itself is
  effectively **stateless** (only soft per-device rate-limit timers live in memory; losing them on
  failover is acceptable — worst case one extra command slips a rate window). No central DB coupling
  for control.
- **Edge nodes are interchangeable.** They carry only Wi-Fi/broker creds + their per-device HMAC
  secret; all behaviour is server-composed. A dead node is swapped by re-enrolling its secret.
- **Security-OTA tiering (ADR-0005 refined, Hugh 2026-06-21):** routine firmware moves OTA-deliverable
  during dev, but the **secure endgame for firmware is cable-flash from the G11** (the "scary" path is
  physical/manual). OTA stays as the convenience/break-glass channel, gated by signed images. Authority
  nodes (lock controllers) lean hardest toward cable-only.
- **Whole-house mode** is a server-owned multiplier over per-tier policy (ADR-0010 / policy.py), so a
  single declared mode (Conserve/Emergency) reshapes the whole fleet's behaviour without per-device code.
- New device shapes are inducted via traits (ADR-0002), not new tiers or new admin code.

## Rejected alternatives

- **Monolithic server owning device-specific logic:** O(n) coupling as the fleet grows; breaks the
  "absorb new hardware without rewrites" goal. Traits + dumb edge nodes avoid it.
- **Smart/autonomous edge nodes holding policy:** distributes authority (split-brain risk, ADR-0001)
  and bloats the hardening surface on the least-trusted tier. Standing orders give the needed
  disconnected autonomy without moving authority off the dictator.
- **Stateful PEP / DB-coupled control:** would make failover a data-migration problem; file-based
  authority keeps promotion trivial.
