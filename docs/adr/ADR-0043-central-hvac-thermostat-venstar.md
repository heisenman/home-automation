# ADR-0043 — Central HVAC via a Venstar ColorTouch thermostat (local API, own schedule, bounded overrides)

**Status:** Proposed — driver, bridge and tests built 2026-10-24 against Venstar's published spec and a fake
unit; **the T7850 is ordered, not installed**. Nothing here is verified live yet (intake §5 is the checklist).
**Relates to:** ADR-0001 (dictator authority), ADR-0002 (traits), ADR-0014 (actuator R1–R6), ADR-0026 (routing
by device_type), ADR-0034 (Node/Ability/Entity), ADR-0041 (Aprilaire + ERV actuators). Intake:
[design/venstar-thermostat-intake.md](../design/venstar-thermostat-intake.md).

## Context

The central HVAC is a Carrier 59TP5A060 two-stage gas furnace (variable-speed blower) plus a single-stage AC,
wired conventionally to a Honeywell RTH6580WF: R(c) · C · W1 · Y1 · G, W2/Y2/DHUM unused (photos hvac_1–4,
2026-10-24). The Honeywell is cloud-only (Total Connect Comfort; no local API), so on the air-gapped network it
can never be controlled. Hugh wants the central HVAC under the system — first motivated by running the furnace
blower whenever the Aprilaire E070 dehumidifies, so more of the house's air reaches it.

## Decision

1. **Replace the Honeywell with a Venstar ColorTouch T7850** (conventional 2H/2C; no humidity control — the
   Aprilaire owns that). Its documented local REST API (v4) needs no cloud. Same five wires, same letters.
2. **One Node, two Entities** (ADR-0034): `thermostat_<area>` (`mode` + `setpoint` with **heat/cool channels**)
   and `hvac_blower` (`switchable`: on = fan continuous, off = auto). The blower is a distinct function in the
   house — the thing a dehumidify coupling would drive — so it gets its own entity rather than a second enum on
   the thermostat. The `setpoint` trait gains optional named `channels` (backward compatible: a single-value
   setpoint is unchanged). Routed by `device_type` `thermostat` / `hvac_fan` to `server/control/venstar_driver.py`;
   telemetry by polling (`server/ingest/venstar_bridge.py`, 30 s), the thermostat's own temperature/RH published
   **non-authoritative**.
3. **The thermostat keeps its own schedule** (Hugh, 2026-10-24). It is the fallback: if ha-2, the network or the
   driver dies, the house is still heated and cooled. The system acts on top of it.
4. **Limits: automation is bounded, people are not** (Hugh, 2026-10-24). Two layers:
   - the trait's per-channel `min/max` = the **manual** range (wide; what a person may set from the PWA/panel —
     plus the unit's own `heattempmin/max`, `cooltempmin/max`, which the driver also enforces);
   - **automation comfort limits** (narrower) live in the automation policy and clamp anything the controller
     sends. A setpoint outside the comfort limits — set by a person in the PWA, on the panel, or **at the wall** —
     is allowed, but raises a **warning** on the PWA and the D1001 (derived from the polled state, so it catches
     every source).
   - **Pinned (not decided):** whether the system ever overrides setpoints/mode by itself. Until decided, both
     entities are `manual: true` (controls, no automation policy).
5. **First automation candidate: the blower follows the Aprilaire** (pinned with §4 — design only). When the
   E070 is called, set `hvac_blower` on; when the call ends, back to auto. The E070 now runs on its own indoor
   loop (ducts split 2026-10-17), so this is about house-wide mixing, not its inlet. Shape it like the old
   ERV coupling (`ventilation` floor in the dehum policy, ADR-0041), with a person's blower override winning.

## Rejected alternatives

- **Keep the Honeywell, bridge to its cloud** — breaks the air gap; breaks whenever Honeywell changes its site.
- **Our own ESP32 thermostat** (24 VAC relays on W/Y/G) — fully ours, but we'd own short-cycle protection,
  heat/cool interlocks and freeze protection for the house's most important equipment, with no wall UI. Later,
  if ever.
- **Z-Wave thermostat** — needs a Z-Wave controller the system doesn't have.
- **A second `mode` enum for the fan on the thermostat entity** — traits are keyed by name (one `mode` per
  entity), and the blower is a real separate function the dehum coupling targets.

## Consequences

- No lease: unlike the Aprilaire's `DH` call, a Venstar setting **sticks** if ha-2 dies. Hence §4's clamp, the
  out-of-limits warning, and the schedule as fallback. A blower left ON is harmless (air moves; ~ blower watts).
- HTTPS on the unit is self-signed; the client does not verify it (the air gap is the boundary). Digest auth +
  the screen-lock PIN are supported and stored in `instance/venstar.env` (docs/SECRETS.md).
- Before go-live: the PWA/D1001 must render channelled setpoints (R1 — every declared trait surfaced) and the
  out-of-limits warning; a static lease on the air-gap router (needs the unit's MAC); install the bridge unit
  (gated — Hugh's copy-paste).
