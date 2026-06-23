# ADR-0014 — Device capability & control conventions

Status: Accepted (2026-06-22). Codifies standing rules learned building the dehumidifier control loop +
PWA. Governs how EVERY future device is onboarded, exposed, and bound to sensors. Builds on ADR-0011
(automation controller), ADR-0012 (comms events), ADR-0013 (presentation).

## Context

We now have the first actuator (Midea dehumidifier) under closed-loop control with a full PWA: manual
command controls, a humidity-source selector, override/policy editing, and sensor graphs. Building it
surfaced recurring decisions that must NOT be re-litigated per device — they need to be standing rules so
both humans and LLMs onboard the next device the same way. Two were named explicitly by Hugh (capability
→ UI exposure; user-confirmed sensor↔actuator binding); the rest fell out of the work (notably: the fan
advertised 3 speeds but is physically 2 — device metadata is a hypothesis, not truth).

## Decision — standing rules

**R1 — Expose every capability to the user.** When a device is onboarded, each declared trait
(`switchable` / `setpoint` / `ranged` / `lockable` / `positionable` / …) MUST be surfaced as a
user-facing control in the UI. No capability ships hidden behind the API. The mechanism is generic: the
view-model carries the device's `traits`, and the UI renders controls from them (number input for
`setpoint`, discrete buttons for stepped `ranged`, toggle/override for `switchable`, …). Adding a trait
to the registry is therefore sufficient to get a control — and is *required* to, not optional.

**R2 — Confirm sensor↔actuator bindings with the user; never auto-wire.** No actuator's control input is
bound to a sensor without the user explicitly approving WHICH sensor(s) are valid sources for that
actuation. The default posture is **ask, don't assume.** Onboarding an actuator that consumes sensor data
(any closed-loop control) MUST prompt the user to choose the source(s); the binding is then user-editable
in the UI (the humidity-source dropdown is the first instance). Corollaries:
- A device's **own** sensor is never an automatic source. It may only be chosen explicitly, and remains
  flagged non-authoritative (R4).
- When a device exposes multiple controllable functions, each function that consumes sensor data gets its
  own confirmed binding (e.g. a future "fan boosts on CO₂" is a *separate* binding from "compressor on
  RH").

**R3 — Verify capabilities live before trusting them.** Device-advertised capabilities (e.g. Midea
`supports: fan_speed(3)`) are a starting hypothesis, NOT truth. Before a capability's controls are
trusted/shipped, verify the actually-accepted values by **reported-state readback** (issue the command,
compare `reported` to `intended`) and by physical confirmation (ear/eye) where it actuates something
audible/visible. Record the verified ranges in `control.yaml` with a dated `# verified live` note.
*Precedent:* the dehumidifier advertised 3-speed but accepts only Low=40 / High=80 (it clamps 60→40 and
ignores 1–3); caught because the issuer's contract gate + reported-state readback disagreed with the send.

**R4 — Trusted sensor vs device self-report discipline.** Sensor readings drive control; a device's own
onboard sensor is **non-authoritative by default** (`authoritative=0`), ingested for visibility but
excluded from area rollups and from being a control source unless the user explicitly elects it (R2).
*Precedent:* the dehumidifier's onboard RH reads 9–15% low and is uncalibratable.

**R5 — Admin-gate all actuation, and verify the credential.** Every command, override, and policy edit
requires the admin bearer (`SHA256("ha-api:"+master)`); sensitive actions additionally require the
confirm token (ADR-0010). The UI MUST verify the credential at unlock (against `/control/auth/check`) and
show success/failure — never store an unverified credential that fails silently later. Read/telemetry
views stay open on the LAN (ADR-0013).

**R6 — Safety precedence is fixed.** Interlocks (tank-full/error) and compressor cycle protection
(min-on/min-off) are not user-tunable away; only `safety` and a manual `override→off` may bypass min-on
(ADR-0011). The UI surfaces interlock state; it never offers a "disable safety" control.

**R7 — Onboarding order is fixed (see docs/device-onboarding.md).** A device is not "done" until, in
order: (1) registry entry — id / area / device_type; (2) friendly name; (3) traits, **verified** (R3);
(4) sensor source binding, **confirmed** (R2); (5) secrets in gitignored `instance/`; (6) UI exposure
confirmed (R1); (7) a seeded default policy the user can edit. Skipping a step is an incomplete onboard.

**R8 — Device identity & lifecycle are user-managed (accepted 2026-06-22; ✅ BUILT 2026-06-23, `e81ff34`).**
Every device has a **user-editable friendly name and room/area**, set from the UI — a user never sees a
raw `device_id` as the only label. User-set names/rooms live in an editable overlay (`control.db`
`device_meta` table) surfaced via the API (`PUT /api/v1/devices/{id}/meta`), NOT by rewriting the
gitignored registry YAML (which stays the source of truth). **Lifecycle shipped so far: rename + room +
hide/restore** (hidden devices drop from the dashboard; an admin "show hidden → tap to restore" recovers
them). **Still TODO under R8:** add-new-device (needs discovery/pairing) and an explicit retire (vs hide)
that also stops automation while keeping history.

**R9 — Access is role-aware, time-bounded, and TLS-protected off-LAN (accepted 2026-06-22; build
queued).** The current single shared master→bearer is an **interim single-admin** model. Target:
- **Roles** — at least **viewer vs admin**, so read-only clients (wall panels, family phones) can see
  everything but cannot actuate.
- **Token expiry + rotation** — credentials are not forever-tokens in `localStorage`; they expire and can
  be rotated without changing the master.
- **TLS for any access beyond the trusted LAN** — the bearer is sniffable pre-TLS (ADR-0010 notes this);
  off-LAN/phone use requires TLS.
Read/telemetry stays open on the LAN (ADR-0013); **actuation requires an unexpired admin credential.**

## Consequences

+ The next device — and the LLM or human onboarding it — follows one path; capabilities can't silently
fail to reach the user, and control can't be wired to the wrong sensor by assumption. + "Add a trait →
get a control" keeps the UI honest as devices grow. + R3 prevents shipping controls that don't match
hardware. − Onboarding has required human-in-the-loop steps (source confirmation, live verification) that
can't be fully automated — by design. − Some rules (R1 generic rendering, R2 per-function binding) imply
UI/view-model work as new trait types appear.

## Open user-control surface (proposed — not yet rules; see gap analysis)

Recorded so they aren't lost; each needs a Hugh decision before becoming a rule or a build item.
(Elevated to accepted rules 2026-06-22: device identity/lifecycle → R8; access/auth → R9.) Still open:
**sensor calibration offsets**; **battery / unreachable / tank-full alerts + delivery (Web Push, MQTT)**;
richer **schedules** (multi-window, day-of-week, time-based setpoints) and **modes/scenes**
(Away/Home/Sleep); **strategy choice (hysteresis vs setpoint) in the UI**; **source fallback chains**; a
**decision-history ("why is it on?") view**; **runtime/duty-cycle budgets**; **per-client/user audit** in
the control log.
