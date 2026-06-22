# ADR-0012 — Communication-event vocabulary + resource abstraction

Status: Accepted (2026-06-22). Event layer built now (immediate payoff); resource registry incremental;
heavy runtime deferred.

## Context

The system spans several transports across three planes:
- **ingest** — BLE adv (host scanner + edge nodes), tagged `transport` per reading
- **reach** — BLE GATT pulls (edge/server), modeled by the mesh link graph + `pull_log`
- **actuate** — MQTT-to-node (signed) + Midea LAN (local driver), behind the issuer `Transport` protocol

Each plane already has a decent per-plane abstraction. What's missing — and currently scattered — is a
*unified notion of connection health/events*: `pull_log` (ok/fail/empty/connect_fail), the issuer's
no-ack/504, the BLE "addr-not-cached" log, broker-unreachable warnings, the Midea token expiry,
stale-sensor. More transports (Zigbee/Matter/more WiFi) are coming. We want a unifying seam **without**
a heavyweight VISA-style runtime (sessions/attribute model) that would impedance-mismatch lossy async
BLE ingest against request/reply command paths.

## Decision

**1. A normalized communication-EVENT vocabulary + bus (build now).** One event set, emitted by every
transport/subsystem, consumed by everyone:

    reachable | unreachable | auth_expired | stale | degraded | acked | no_ack | refused

`CommsEvent(ts, device_id, transport, kind, detail)` → published to MQTT `home/_event/<device>` AND
persisted to a `comms_event` table. Consumers: the **controller** (fail-safe on `stale`/`unreachable`),
the **mesh router** (reroute on `unreachable`), the **UI** (health badges) — all transport-agnostic.
This has immediate MVP payoff: the controller needs `stale`/`unreachable` for its fail-safe regardless.
Mappers translate existing signals (`pull_log` outcome, issuer `Result.status`) into the vocabulary so
nothing is rewritten — they just also emit a normalized event.

**2. A resource/addressing registry (incremental).** One resolution `device_id -> (plane, transport,
driver, address, creds)`, generalizing the issuer's `RoutingTransport` + the scattered registries
(control.yaml, devices.yaml, mesh links, midea-device.env). Adding Zigbee/Matter becomes "register a
driver + address," not a new code path in five places. Grow it as transport #3 lands.

**3. Defer the heavy VISA runtime** (sessions, attribute model, formatted I/O) until a third *actuation*
transport reveals the real common shape. Lay the seam, don't pour the concrete.

## Consequences

+ Connection health becomes a first-class, queryable, transport-agnostic fact (one vocabulary, one bus).
+ The controller fail-safe, mesh reroute, and UI health all read the same events. + The event bus is
also the presentation real-time feed (ADR-0013) — one spine. − A new `comms_event` table + the
discipline of emitting events from each transport. The resource registry and heavier abstraction are
explicitly incremental, not big-bang.
