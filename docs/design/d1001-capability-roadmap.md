# D1001 capability roadmap — what it should do next, and how

**Date:** 2026-07-04  **Status:** Plan (decompose-before-dev; no code until an item is picked up).  **Owner:** ops.
**Source:** the D1001 conformance review (2026-07-04) — the first walk of a device through
[CONFORMANCE.md](../CONFORMANCE.md). Method: [DEVICE-INTAKE.md](../DEVICE-INTAKE.md). Amends
[ADR-0019](../adr/ADR-0019-screen-interface-architecture.md).

## Why this exists

The D1001 runs a **fraction of its silicon.** [`bsp_display.h`](../../provisioning/reterminal/beachhead/main/bsp_display.h)
states the firmware "drops esp-sr/codec/cam/IMU/RTC" — so an **IMU, a hardware RTC, an audio codec/mic, and a
camera are on-board but never brought up.** Walking the D1001 through the ability catalog (A–K) shows which
abilities it exercises today and which its hardware positions it for. This is that backlog, decomposed
module-first so each item is pick-up-ready.

> **DOCS-FIRST GATE (blocks items #1/#3/#6).** The IMU/RTC/codec/camera are asserted from the `bsp_display.h`
> "drops" note + Seeed's BSP. Before writing a line against any of them, **read the D1001 schematic**
> (`~/Desktop/Profile/home_automation/docs/D1001_Docs/`) + the Seeed BSP for the exact part, I2C address, and
> pins. (Precedent: the es7210 audio ADC was mis-ID'd as a current monitor once — confirm, don't infer.)

## The D1001 today (from the conformance review)

**Exercises:** A (self-diagnostic telemetry), D (BLE relay + `ha_reach` census), E (display/panel — thin BFF
renderer), F (battery — the ADR-0024 reference impl), H (data-of-record / `ha_replica` Phase 2), I (OTA), plus
the **control-client** side (R5 UI-clause: `ui_admin` verifies the admin credential at unlock).
**Deliberately not:** B (actuator), C (signed directives), G (clock), J (comms-event vocab), K (cluster box).

---

## The roadmap (prioritized)

Each item: **Ability** it fills · **Decompose** (shared module vs device glue — the ADR-0020 seam, named
before code) · **Reuse** (what already exists) · **Conformance** (SHALL items it must meet) · **Test** · **Deps**.

### SHOULD #1 — Wall clock: bring up the RTC + SNTP  ▸ ability G
The D1001 is the house's primary always-on display and has a **hardware RTC unused** — which is *why* it's
clockless. With chrony now live on .210, add SNTP + RTC → a wall clock that **survives reboots on RTC holdover**
(better than the E1001, which has no RTC).
- **Decompose:** NEW shared component **`ha_rtc`** (RTC read/set + SNTP discipline; board I2C handle + address
  injected via cfg — a second panel will want it) · **device glue** = an LVGL clock element in `ui/` + wiring
  SNTP to the RTC on WiFi-up.
- **Reuse:** the **E1001 SNTP pattern** (`e1001.yaml time: sntp` → .210, `.is_valid()` guard) — mirror the
  discipline; `ha_reach`/`ha_sdcard` show the "board handles injected via cfg" shape to copy.
- **Conformance (G):** sync from the LAN time authority (.210 holdover); degrade gracefully when unsynced.
- **Test:** host test for the RTC↔epoch math; on-device **holdover-across-reboot** (set via SNTP → power-cycle →
  clock still right from RTC before WiFi returns).
- **Deps:** none (unblocked). **Unblocks #4.**

### SHOULD #2 — Panel display as a trait-based actuator  ▸ ability B
**✅ CODE SHIPPED (interim/unsigned, fw `v60-panel-actuator`).** Registration + live-validate pending →
[d1001-panel-actuator-registration.md](d1001-panel-actuator-registration.md).
Today `cmd/screen` (on/off/brightness) is a bespoke ops command. Expose it as `switchable` + `setpoint` traits
so the **Sleep scene dims/kills the panel through the normal PEP**, not a side channel — making the panel a
first-class controllable *device*.
- **Decompose:** mostly **registration + glue**, no new shared module. Register the panel in `devices.yaml`
  (`device_type: panel`, `traits: {switchable, setpoint(brightness 0–100)}`) · device glue = a control-command
  handler that maps an incoming set→`bsp_display` brightness/power.
- **Reuse:** ADR-0002 trait vocabulary + the **existing server control plane** (issuer/PEP, `vm.controls`) —
  the server already commands traits; the panel just becomes a target. No new server endpoint.
- **Conformance (B/R1,R5,R6):** every declared trait gets a control (R1); admin-gated (R5); interlocks fixed (R6).
- **⚠️ Dependency subtlety:** a controllable actuator *receives* commands — which per ADR-0010 should be
  **signed** (ability C). So the *proper* form of #2 needs **#4 (enrollment)**. **Interim:** accept
  scene-driven display commands over the existing unsigned-LAN `cmd/` path (posture-consistent with today's
  `cmd/*`); promote to signed when #4 lands. Note this tension explicitly in the impl.
- **Test:** Sleep scene → panel dims; admin-gate a manual brightness set; verify in PWA + `/api/v1/displays`.
- **Deps:** proper form needs #4; interim form standalone.

### SHOULD #3 — IMU motion → presence + tap-to-wake  ▸ extends ability A
The IMU is used only for temperature. Its accelerometer gives **tap/motion-to-wake** and, higher value, a
**room presence/activity signal** — "someone's at the panel" is a strong occupancy input for automations.
- **Decompose:** NEW shared component **`ha_imu`** (accel read + motion/tap detection; I2C handle + threshold
  cfg injected — the E1001/future panels can reuse) · **device glue** = publish `home/edge/<node>/presence` +
  a tap→`bsp_display_wake()` hook.
- **Reuse:** the **edge event envelope** (`home/edge/<node>/event`, schema 1) for the presence publish — mirror
  `power_ctx_publish`; the coordinator's `edge_mapper` already consumes `home/edge/+/`.
- **Conformance (A):** keyed by identity; presence is an edge signal to the dictator (which owns the mapping) —
  the panel doesn't interpret it.
- **Test:** motion threshold tuning (no false-positives from HVAC vibration); tap-to-wake latency; presence
  event on approach.
- **Deps:** none. Needs the docs-first IMU part/pin confirm.

### SHOULD-CONSIDER #4 — Enroll the `cmd/*` surface  ▸ ability C
The review found `cmd/ota`/`cmd/fs`/`cmd/gpio` are **unsigned** (LAN+ACL only — [beachhead_main.c:255](../../provisioning/reterminal/beachhead/main/beachhead_main.c#L255)
flashes any URL; no HMAC anywhere). Enrolling the panel (per-device secret + HMAC + freshness) closes it.
- **Decompose:** bring the **signed-directive verify** to the panel as a shared module (promote the edge nodes'
  HMAC/`ha_cmd`+`gatt_exec` verify path — `(ts,seq)` NVS anti-replay + freshness) · device glue = gate each
  `cmd/*` handler behind it.
- **Reuse:** the edge nodes already implement ADR-0010 verify — **reuse, don't reinvent** (this is the exact
  reuse the catalog exists to force).
- **Conformance (C):** signature (constant-time) + freshness + nonce-non-replay; per-device secret enrolled at
  the server console; refuse on any failure.
- **Test:** replay a captured command → refused; stale-timestamp → refused; valid → acts.
- **Deps:** **#1 (clock, for freshness)**; posture decision **`broker-auth-posture`** (is signing mandated on
  the LAN, or is ACL enough?). Flag-and-decide with Hugh, not a silent build.

### COULD #5 — C6 as a full BLE *gateway*  ▸ deepens ability D
Today the D1001 passively relays adverts; the C6-over-`esp_hosted` HCI path lets it do **active GATT pulls +
downlink command-relay** — a real edge gateway. Already the open board task **`ble-edge-node`** (ADR-0019 Phase
6, Spike 0 first).
- **Decompose:** reuse the edge `gatt_*` modules over the P4↔C6 HCI transport; device glue = the panel-side
  gateway task.
- **Reuse:** `ha_ble_scan`, `gatt_history`, `gatt_exec` (edge forks → future shared `ha_gatt`).
- **Deps:** needs Hugh's steer (still wanted, or superseded by the switchbot relay?). Biggest lift.

### COULD #6 — Audible / visual alerting  ▸ new surface on ability E
The always-on panel is an ideal house **alert surface** — critical alerts → chime (via the dropped codec) +
screen flash, not just a silent banner.
- **Decompose:** NEW glue **`ha_audio`** (codec/es7210 bring-up + a tone) · device glue = trigger on
  `g_alert_crit`.
- **Reuse:** the alert-banner logic already consumes `/api/v1/alerts`; just add an actuation on critical.
- **Deps:** docs-first codec part/pin confirm.

### COULD #7 — Deepen the recovery role  ▸ ability H
The original vision (memory) had the D1001 as a fuller offline backup of the dictator `instance/` (config +
parquet + `hot.db`), not just the rung ladder. `ha_replica` could grow toward that warm-standby-on-SD role.
- **Decompose:** extend `ha_replica` with a config/parquet replica lane (source-tagged, ADR-0018 gate-aware).
- **Deps:** diminishing returns vs the rung ladder; sequence last.

---

## Sequencing

```
#1 clock/RTC ──┬─▶ #4 cmd enrollment ──▶ #2 (proper, signed panel-actuator)
               │        (+ broker-auth-posture decision)
#3 IMU presence (parallel, independent)
#2 (interim, unsigned) can ship before #4
#5 C6 gateway (independent, big — needs Hugh's go)
#6 audible alerts (independent, small)   #7 deepen recovery (last)
```

**Recommended first tranche:** **#1 (clock/RTC)** and **#3 (IMU presence)** — high-value, well-scoped, no
posture decision; #1 also unblocks the security path. **#2 interim** is a quick win. #4/#5 are decisions for
Hugh (posture / is-the-gateway-wanted). Each item is additive and independently OTA-able (the ADR-0019 layered
discipline); build one, validate on `.8`, ship.
