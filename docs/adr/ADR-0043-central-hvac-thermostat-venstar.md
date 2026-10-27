# ADR-0043 — Central HVAC via a Venstar ColorTouch thermostat (local API, own schedule, bounded overrides)

**Status:** Proposed → **in service 2026-10-26** for §1–§3 and §5 — the T7850 is installed (hallway), its local
API verified live (intake §5 results), the PWA renders its controls and the comfort warning, and the blower
follows the Aprilaire. §4's automatic override question stays **pinned**.
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
   - Comfort defaults (proposed 2026-10-26, Hugh to adjust): heat 62–74 °F, cool 72–82 °F — the `comfort` of
     each setpoint channel in control.yaml. A warning fires only for the channel the current mode uses.
   - **Pinned (not decided):** whether the system ever overrides setpoints/mode by itself. Until decided, both
     entities are `manual: true` (controls, no automation policy) — the §5 follow is a coupling on the blower,
     not a setpoint/mode override.
5. **The blower follows the Aprilaire** (Hugh, 2026-10-26: "whenever the attic dehum turns on so does the
   blower"). Configured on the blower's switchable trait (`follow: {device: dehum_attic}`), implemented in
   `server/control/follow.py` + `Controller._apply_follow`. **Edge-triggered**, so a person always wins: on the
   Aprilaire's call START the blower goes On unless it already was (then it's a person's setting, left alone);
   on the call STOP it goes back to Auto only if we turned it on and it is still on. State persists in control.db
   (`follow:hvac_blower`), so a restart mid-run still hands it back. Unknown state (stale telemetry) → no action.
   Not done: the ERV-style `ventilation` floor (levels, dry-out) — the blower has no levels, and the E070 now sits
   on its own indoor loop, so there is no coil to dry with house air. Open: running the blower right after an AC
   cycle re-evaporates water off the wet coil; if summer RH shows it, gate the follow on "no cooling in the last
   N min".
6. **UI (R1)** — the PWA's manual control renders a channelled setpoint as one field per channel (posting
   `{channel, value}`), a manual switchable as two labelled buttons (blower On | Auto), and current values from
   the metric each control names (`now_metric`, °C shown in °F). The comfort warning is a device `alert`
   (amber banner) and a `comfort_limits` entry in `/api/v1/alerts` (ntfy, E1001). **The D1001 has no alert
   reader yet** — FOLLOWUPS.

## Rejected alternatives

- **Keep the Honeywell, bridge to its cloud** — breaks the air gap; breaks whenever Honeywell changes its site.
- **Our own ESP32 thermostat** (24 VAC relays on W/Y/G) — fully ours, but we'd own short-cycle protection,
  heat/cool interlocks and freeze protection for the house's most important equipment, with no wall UI. Later,
  if ever.
- **Z-Wave thermostat** — needs a Z-Wave controller the system doesn't have.
- **A second `mode` enum for the fan on the thermostat entity** — traits are keyed by name (one `mode` per
  entity), and the blower is a real separate function the dehum coupling targets.

## Consequences

- **2026-10-25/26: the furnace ran while the unit reported Off.** First read as a failed W output; the real cause
  is off-state leakage from the solid-state W output into the furnace's high-impedance W input. Fixed with a RIBU1C
  isolation relay at the furnace (2026-10-26 evening PDT; proven on the original unit). Consequence for the design: the
  thermostat's own report is NOT evidence of what its wires do. Before any automation touches heat/cool, add an
  independent furnace call monitor (24 V sense on R/W/Y/G + supply-air temperature) and alarm on "burner/W live
  with no call reported". Intake §5 has the diagnosis.
- No lease: unlike the Aprilaire's `DH` call, a Venstar setting **sticks** if ha-2 dies. Hence §4's clamp, the
  out-of-limits warning, and the schedule as fallback. A blower left ON is harmless (air moves; ~ blower watts).
- HTTPS on the unit is self-signed; the client does not verify it (the air gap is the boundary). Digest auth +
  the screen-lock PIN are supported and stored in `instance/venstar.env` (docs/SECRETS.md).
- Before go-live: the PWA/D1001 must render channelled setpoints (R1 — every declared trait surfaced) and the
  out-of-limits warning; a static lease on the air-gap router (needs the unit's MAC); install the bridge unit
  (gated — Hugh's copy-paste).
