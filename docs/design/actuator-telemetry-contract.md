# Actuator telemetry / area-stamping contract (design note — for dev review)

**Status:** PROPOSED, for dev review. Prompted by Hugh (2026-07-06) after the 2nd–3rd recurrence of
"Midea does things one way, Levoit does things another." Ops-authored from a read of `server/control/`;
implementation shape is dev's call (dev owns the control layer).

## The one-line problem
The **command/control path is unified; the telemetry/status path is not.** Each actuator family gets its
live state *into* the system — and stamps its `area` — through a **different mechanism, in a different
place**. That asymmetry is the direct cause of a recurring bug class where a fix to one family silently
misses the other.

## What IS already unified (keep this — it's good)
- One control loop (`controller.tick`), one policy + ACL layer, one signed **`CommandIssuer`**.
- Outbound commands are abstracted by **`RoutingTransport`** (`bootstrap.py`): `device_id` → backend
  transport — Midea LAN → `MideaTransport`, Levoit → `LevoitMqttTransport`, BLE → `MqttTransport`, host
  LEDs → `HostLedTransport`. "Tell device X to do Y" conforms to a common contract regardless of family.

## What is NOT unified (the gap)
The **inbound state / telemetry path** has two structurally different models with no shared contract:

| | **Midea (dehumidifier)** | **Levoit (purifier)** |
|---|---|---|
| State source | controller-side **local driver** `MideaDriver.status()` — the controller *polls* it each tick (`controller._tick_device`, ~L181) | **driverless** — state is *pushed* by a standalone `levoit_bridge.py` ingest process (controller calls it "driverless MQTT device", ~L192) |
| Who publishes state + stamps `area` | **`ha-controller`** (`_publish_state`, `transport: "midea-lan"`, area added per 811a349) | **`levoit_bridge`** (stamps area from the registry at ingest) |
| Area-stamping site | inside the control loop | inside a separate ingest bridge |

So "where does a device's location come from" has **two different answers** depending on family, and the
stamping code lives in two places that must be kept in sync by hand.

## Evidence (why this is not academic)
The device-relocate "pop-back": dev's registry live-reload (`registry_reload.RegistryReloader`) was wired
into the **5 ingest bridges** (scanner/edge_mapper/edge_history/tasmota_bridge/**levoit_bridge**), so a
Levoit relocate would stick. It was **not** wired into **`ha-controller`**, so a Midea relocate popped
back — confirmed live: `dehumidifier_living_room` readings went `hall @19:28:25 → living_room @19:29:11`.
Same bug class, two stamping sites, fix one and miss the other. (`controller-area-reload` is the current
point-fix; this note is the structural fix so there is no next time.)

## Proposal — a single telemetry/status contract
Define **one interface every actuator's live state flows through**, so identity + `area` are stamped in
**exactly one place**, from the **canonical registry** (honoring the mtime-reload), regardless of whether
the device is a polled local driver or a pushed bridge.

Design constraints:
- **Area + identity are stamped by the contract, not by each family.** A family driver/bridge reports raw
  device state (running, fan, target, onboard reading); the shared layer attaches `device_id`, `area`
  (registry-resolved, reload-aware), `transport`, trust level, and publishes. Neither family can drift on
  location because neither family computes it.
- **Keep the command abstraction as-is** (`RoutingTransport` + `CommandIssuer`) — it already conforms.
- **Both models remain allowed** (poll-a-local-driver vs consume-a-bridge) — the contract is about the
  *output shape + stamping*, not about forcing every device into one transport.
- One obvious home for the shared stamp is the same place `writer.py` already denormalizes `area`; the
  contract should make the *producers* consistent so the writer never sees a family-specific shape.

Options for dev to weigh (not prescribing):
1. **A `StatusSource` protocol** both the Midea local-driver path and the Levoit bridge path implement,
   with a shared `publish_state()` that does the identity/area stamping centrally.
2. **Route all actuator state through one publisher** (the controller consumes bridge telemetry AND local
   driver status, then a single `_publish_state` stamps + emits for every family).
3. Minimal: extract the area/identity stamping into one shared helper both `ha-controller` and the bridges
   call — smallest change, still removes the two-sites-to-keep-in-sync hazard.

## Non-goals
- Not re-architecting the command path (already unified).
- Not forcing Midea and Levoit onto the same transport — heterogeneous backends are fine; only the
  **state output + stamping** must conform.

## Ask
Dev: review + pick a direction (or push back). If we adopt one, it likely graduates to an ADR. The
immediate `controller-area-reload` point-fix still ships regardless; this note is about closing the seam
so location-stamping stops being whack-a-mole across families.
