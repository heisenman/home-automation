# Panel UI redesign — spatial navigation (house → room → device)

**Date:** 2026-07-06  **Status:** Planning / decompose-before-dev (no renderer code yet). **Owner:** ops.
Builds on [shared-ui-spec.md](shared-ui-spec.md) (BFF = single UI-truth source) and
[ADR-0026](../adr/ADR-0026-canonical-area-taxonomy.md) (level → zone → room taxonomy). Pairs with the
already-done [panel-ui-modularization.md](panel-ui-modularization.md) (the `main/ui/` module split this
redesign extends).

> **Mockup (approved):** the house-map → room-zoom → device-graph flow, drawn on the real to-scale floor
> plan with live data — Claude Artifact `3123e374-4b5b-489f-bd3a-d590720c4ae4`. This doc formalizes that
> mockup into data contracts + a module plan. Real house geometry / device coordinates are **private**
> (gitignored `instance/`, canonical on 210); only the *schema* and *contracts* live here.

## The model (locked by the mockup)
Navigation is **spatial**, not a flat grid:

1. **House map** — the floor plan; every room glanceable. A populated room shows its **headline live reading**
   + a device-count marker. Tap a room →
2. **Room zoom** — the room enlarged; its devices drawn at their **geometric position**, labeled, with live
   values inline. Tap a device →
3. **Device graph** — the reading over time (server-windowed), with `+ builder` to add it to the cross-source
   graph builder.

Cross-cutting (unchanged from today, carried forward): **category views** (all-humidity / by-type / by-room),
the **graph builder**, **per-device settings**, and persistent **chrome** (clock / wifi / battery / online
count). Attic & crawlspace are monolithic **levels**, reached via a level switcher — they have no main-floor
polygon.

**Overrun rule** (from the spec): list devices inline until they overrun the room box, then collapse to
`n sensors / m actuators / o edge nodes`. *Reality:* the densest room today holds **2** devices, so the
collapse is dormant — implement the rule, but it won't fire until rooms get denser. Threshold is an open
decision (by count, or by whether labels physically fit the polygon).

## Surface reality (why this is two renderers, not one UI)
Per shared-ui-spec: nothing shares C between an ESP-IDF panel and an ESPHome panel — they share the **BFF
contract**, not code. So this redesign = **the shared data/API contracts below + two thin renderers**.

- **D1001** (LVGL, 800×1280 portrait color touch) — the full spatial model. Floor-plan aspect ≈ the screen.
- **E1001** (ESPHome, 800×480 1-bit ePaper, 3 buttons, slow refresh) — spatial/live does **not** fit; stays
  a **list** (house → room → device via buttons) or a static room-outline with counts. Depth is an open
  decision.
- **PWA** — already does everything (incl. the graph builder); consumes the new room/geometry contract for
  its house/room nav.

## Data contract 1 — house geometry  (`instance/house-geometry.json`, private)
Machine-readable render source, extracted from the hand-authored `instance/house-geometry.html` (the to-scale
SVG). **This file is the dependency the map could not be built without** — before it, room polygons existed
only as hand-drawn SVG.

```jsonc
{
  "schema_version": 1,
  "space": { "units": "svg_px", "width": 810, "height": 1410, "px_per_ft": 22, "north": "up" },
  "outer_wall": [[x,y], ...],
  "rooms": {
    "<area_id>": {
      "name": "…", "level": "…", "zone": "…", "type": "…",   // joined from areas.yaml
      "poly":  [[x,y], ...],        // single polygon, house-space units …
      "polys": [[[x,y],...], ...],  // … OR multiple (composite rooms, e.g. an L-shaped circulation space)
      "label": [x, y],              // label centroid
      "exterior":   true,           // dashed outline (e.g. porch)
      "monolithic": true,           // a level, no main-floor polygon (attic / crawlspace)
      "confidence": "inferred|tentative"  // carries the source SVG's own amber/"later" flags forward
    }
  }
}
```
- **Coordinate space** = the source SVG's viewBox (house units), N=up (y grows south). Renderers scale to fit.
- **Join key** = `area_id`, the ADR-0026 canonical slug. Room *metadata* stays in `areas.yaml` (not duplicated);
  this file is pure shape.
- **Confidence flags** are preserved from the source SVG so a reviewer knows which polygons are surveyed vs
  inferred; they are advisory, not rendered.

## Data contract 2 — device placement  (`instance/device-placement.yaml`, private)
Per-device position **within a room** — the room-zoom view (mockup frame 2) needs it; the house-map counts and
tap→graph do **not**.

```yaml
placements:
  <device_id>: { x: <0..1|null>, y: <0..1|null>, anchor: n|s|e|w|auto }
```
- **`x,y` normalized to the room's bounding box** (0=W/N edge, 1=E/S), *not* the house — so a device pin
  survives room re-scaling and edits to the house geometry.
- **`anchor`** = which side the label leans, to keep callouts off the walls (default `auto`).
- **Opt-in**: `null` coords → the device renders in the room's fallback **list**, not on the geometric view.
  Rooms with 0–1 devices never need placement.
- **Physical positions are not derivable** — they are captured with Hugh (walk-the-house, tap each device on
  the room-zoom view, same as the area capture). The file ships **seeded with the real roster and `null`
  coords** (capture-ready), never fabricated.

## Data contract 3 (keystone) — the house/rooms API endpoint
The one piece of **server code** that unblocks every renderer. Today `/areas` returns bare distinct strings
from live devices only; `/api/v1/house` is scene state (misnamed). Neither exposes level/zone/name/order,
empty rooms, geometry, or a room→devices grouping. This endpoint is the deferred **ADR-0026 "Phase 3 (UI)"**.

**Proposed** `GET /api/v1/rooms` (name TBD) → for each canonical area:
- `id, name, level, zone, type, order` (from `areas.yaml`),
- `geometry` (polygon(s) + label + flags, from `house-geometry.json`),
- `devices[]` grouped into the room, each with `device_id, device_type, role (sensor|actuator|edge),
  placement (x,y,anchor), headline_metric` (the glance value), and
- `counts { sensors, actuators, edge }` (for the overrun collapse).

Live *values* keep coming from the existing `/api/v1/sensors`; history from `GET /devices/{id}/readings`
(already server-windowed, `res=auto` downsampled — panel-friendly, unused by the PWA builder today). No new
history work.

## Module plan (4 workstreams, spec-first)
1. **Server keystone (ops)** — the `/api/v1/rooms` endpoint above: load `areas.yaml` + `house-geometry.json` +
   `device-placement.yaml`, join with the live device list, emit the room graph. Additive; unit-tested in
   `server/`. *This is the unblocker — do first; good candidate to fan out to a subagent.*
2. **D1001 renderer (LVGL, ops)** — a **nav stack** (House-map → Room-zoom → Device) replacing the flat grid,
   on the existing `main/ui/` modules: `ui_map` (floor plan + chips), `ui_room` (zoom + placed devices),
   reusing `ui_chart`/`ui_expand`/`ui_controls`. Theme tokens (kill inline hex), two-row chrome.
3. **E1001 renderer (ESPHome, ops)** — house → room → device paging + button drill-down; mono room-outline +
   counts. Read-mostly. Depth = open decision.
4. **PWA (ops)** — consume `/api/v1/rooms` for house/room nav + category views; **preserve the graph builder**.

Actuator controls (render-mode hint on `vm.controls`, the third shared-ui-spec gap) stay **deferred** until
the house + sensor-category views land — per Hugh.

## Open decisions
- **House-map glance value:** headline reading (mockup) vs device-dot cluster vs both.
- **Empty rooms** (dining nook, mud room, closets): greyed vs hidden-until-populated.
- **Overrun threshold:** device count vs label-fit.
- **Level switcher:** top-left chip (mockup) vs bottom nav.
- **Actuator marker:** amber dot (mockup) — enough, or need inline fan/relay state on the map.
- **E1001 depth:** flat list vs room-outline+counts.
- **Placement schema:** points-first (current) vs footprints for devices with extent (purifier/plug).
- **Low-confidence geometry:** confirm the `inferred`/`tentative` rooms + capture the placement-only
  `h_bed_closet` with Hugh.

## Data-home / provenance
`house-geometry.json` + `device-placement.yaml` are **non-committed instance data**: canonical on 210,
authored through the ops sshfs mount (`~/mnt/ha210/instance`), mirrored 210→245 by `sync-standby.sh`
(add manifest rows via `house-geo-backup-sync`), and part of the edge-SD replica lane. The public repo carries
**only** this doc's schema + contracts — never the real geometry, room list, or coordinates.
