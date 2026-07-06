# Adaptive room-fill for the house map (design note)

**Status:** DESIGN, approved by Hugh 2026-07-06. Not yet implemented. Captures the original room-fill schema
(which got lost during the relocate/device-management work) so it is durable this time.

## Goal
On the house-map view, each room's polygon shows **as much live device data as its rendered area can hold**,
degrading gracefully when the box is small and — the inverse — *upgrading* when the box has surplus space.
Legibility at wall-panel distance is the constraint; we degrade by **tier**, never by shrinking the font.

## The tier ladder (greedy — render the richest tier that fits)
Selection is per-room, recomputed on render, against the room's **usable rectangle** (below):

| Tier | Content | Rule |
|---|---|---|
| **0a — everything** | one line per device, **all** its metrics (temp/hum/CO₂/PM…, full actuator state) | show if the complete set fits |
| **0b — all devices, core metrics** | still **every** device, but drop secondary metrics (CO₂/PM); keep temp/hum + actuator state | complete device list still fits when 0a doesn't |
| **1 — climate glance + counts** | room climate value + `n s  m a` | list won't fit but a climate read will |
| **2 — counts** | `n s  m a` | today's floor |
| **3 — icon + badge** | a dot with a number, no text | box too small even for counts |
| **S — surplus** *(future)* | richer than 0a: larger glanceable values, trend arrows, mini sparkline | usable area ≫ content (attic/crawlspace) |

- **All-or-nothing on the device list** (Tiers 0a/0b): the list is always complete or absent — never a partial
  list with `+N`. Metric richness may degrade (0a→0b); the *set of devices* may not.
- **Surplus tier (S)** is the inverse of the ladder: the same fit engine that degrades when
  `space < content` can promote when `space > content`. Attic and crawlspace are tall, sparse rooms with a
  lot of vertical room — natural test beds. Captured as an opportunity; not v1.

## The seam: BFF authors content, panel owns fit
The fit decision is **geometry** (rendered polygon px + font line-height) — only the **panel** knows it. What
to show — the per-device value strings, the climate summary, the counts — is **content** and belongs on the
**BFF**, pre-formatted and priority-ordered. This is the shared-ui-spec merge boundary (ADR-0019 direction):
the BFF starts authoring the per-room render payload and the **PWA gets the same data for free**.

- **BFF** (`server/api/viewmodel.py` `build_rooms`) emits, per room, a ranked render payload (details below).
- **Panel** (`ui_map.c`) measures each candidate tier with `lv_txt_get_size()` against the usable rect and
  renders the richest that fits.

## Climate representative (per room)
1. **Primary configured** → show it, normal color.
2. **Primary stale/offline** → fall to **secondary** (that is what secondary is for — a fallback).
3. **Neither configured** → **mean** of the room's sensors, tinted **amber** = "un-curated." Escalate the
   tint / add a `⚠` when the sensors **diverge past a threshold** — an average hiding a real spread is the
   actual "worth investigating"; two agreeing sensors averaged are harmless.

`climate_role` (`primary` | `secondary`) is a **device-meta field** on the sensor, reusing the existing meta
overlay (same plumbing as relocate/status). Authored first in `instance/` config (canonical on the dictator),
later settable from the devices screen. The map degrades gracefully (amber mean) until roles are set, so this
is **not on the critical path**.

## Semantic color (v1)
- **Averaged-climate tint** (amber; escalates on divergence) — above.
- **Out-of-range values → red.** Any metric past its configured normal range renders red, per-metric. Same
  BFF-flag / panel-color seam: the BFF marks the value out-of-range, the panel colors it.
- Both are separate from any room-fill accent; this is state color, not decoration.

## Usable rectangle (fit input)
Text needs a rectangle. Rooms are not all rectangles (the closet carve; possible L-shapes), and today
`room_screen_bbox()` uses the raw **bounding box** — which overestimates non-convex rooms and, worse,
`make_label` then **clamps to 150×74 px**, an artificial ceiling that blocks Tier 0 in big rooms and the
surplus tier entirely.
- **v1:** keep the bounding box but **remove the clamp** and inset for the name label + margin. Good enough
  for the mostly-rectangular room set.
- **v2:** compute the **largest inscribed axis-aligned rectangle** (cheap interior grid scan at these sizes)
  for honest non-convex fit.

## Icons (default, gated on one font)
Icons for metric units (🌡/💧/CO₂) and device-type row leads buy the most horizontal space and read best at
distance → **default to them**. Dependency: the panel currently compiles only Montserrat 14/20/28 (no
thermometer/droplet/CO₂ glyphs). Icons require compiling an **icon font** (FontAwesome subset via
`lv_font_conv` into LVGL) or shipping small images. Scoped as its own small step; text-label v1 can ship
first and swap to icons after.

## Panel implementation (`ui_map.c`)
Generalize the existing `make_label` (which is already a degenerate 2-tier: name + `show_2nd`) into a **tier
fitter**:
1. Build the ranked line set for the room from the BFF payload (device lines 0a/0b, climate, counts).
2. From the richest tier down, measure with `lv_txt_get_size()` (font fixed) against the usable rect.
3. Render the first tier that fits; Tier 3 (icon+badge) is the floor.
Preserve the queue+worker discipline (no blocking work on the touch/mqtt stacks). `room_screen_bbox()` stays
the geometry source (v1), gains the inscribed-rect option (v2).

## BFF payload additions (`build_rooms`) — the dev half
`build_rooms` already emits per-room `devices[]` with `metrics` + `age_s`, `counts`, and `geometry`, so the
Tier-0 raw data is largely already on the wire. Additions:
1. **`room.climate`** = `{ value:{temperature_c,humidity_pct}, source_device_id, confidence }` where
   `confidence ∈ {primary, secondary, averaged, averaged_divergent}`. Resolves `climate_role` meta →
   primary/secondary; else mean + divergence check.
2. **Per-metric range flag** on each device metric (or a parallel map): `out_of_range: bool` against the
   metric's configured normal range, so the panel colors red without re-deriving ranges.
3. **Metric tier tag** — mark which metrics are **core** (temp/hum, actuator state) vs **secondary**
   (CO₂/PM) so the panel can do the 0a→0b drop deterministically instead of guessing.
4. **Stable device-line ordering** (actuators-then-sensors, or a defined order) so the greedy fit and the PWA
   agree on what drops first.

Server stays the authority on ranges/roles; the panel never re-derives them. Unit-test the climate resolution
(primary / stale-primary→secondary / averaged / divergent) and the range flags in `server/`.

## Verification
- Small room (closet) → Tier 2/3, never overflows the polygon.
- Mid room → climate glance shows primary (normal), or mean (amber) when unconfigured; amber escalates when
  sensors diverge.
- Big room → full per-device lines once the 150×74 clamp is gone.
- Out-of-range metric renders red on both panel and PWA.
- Attic/crawlspace → surplus tier (when built) uses the vertical room instead of capping.

---

# Arc 2 — actuator state shown spatially (design note)

**Status:** panel shipped (D1001 v91, defensive); BFF half is the dev item below. Approved by Hugh 2026-07-06.

## Goal
An actuator on the map currently shows its onboard *reading* (the dehumidifier shows humidity) or just its
name — never whether it is **acting** (running/idle, and at what setpoint/level). That is the missing half of
the room glance: you can see the climate but not what is doing something about it. Arc 2 shows **action**.

## Decision — actuators answer "what is acting", sensors answer "what is measured"
- **Actuator row** = `‹glyph› ‹name› ‹status›` — the running glyph + a short server-authored status string
  (target/level/mode). It does **not** repeat its onboard reading; the room **climate line** already covers
  ambient, and two humidity numbers (onboard vs target) read as clutter.
- **Glyph + color** encode running state: `LV_SYMBOL_PLAY` + green when running, `LV_SYMBOL_PAUSE` + dim
  slate when idle. (Both glyphs are in the compiled Montserrat-14 FontAwesome subset — no icon-font work.)
- **Color priority:** out-of-range **red** > running **green** > idle **dim** > sensor **normal**.
- Tinted room fills were **rejected** (Hugh, 2026-07-06): colorized text already conveys state; not worth an
  `lv_canvas` + PSRAM budget for a fun-but-already-working feature.

## Panel (shipped, defensive)
`ui_map.c`: `dev_is_actuator()` + `dev_color()` (priority above) + a `dev.state` branch in `dev_line()`.
Reads `dev.state {running, status}`; when absent it **falls through to the metric line** (no regression), so
it lights up the moment the BFF ships state — same pattern as `room.climate`.

## BFF half (the dev item)
Add per-actuator **live state** to `build_rooms` devices[] (role == "actuator"):
```
"state": { "running": bool, "status": "<short string>" }
```
- **`running`** — actively actuating (compressor/fan/plug on).
- **`status`** — a short, **server-formatted, family-agnostic** detail string (dehum → target RH e.g. "45%",
  purifier → fan level e.g. "Fan 2", plug → "on"/""). The panel renders it verbatim (ASCII-folded) — it does
  **not** re-derive per family, exactly as with the room-fill content.
- Source it from the **same actuator-state logic** that `build_actuator_list` / `build_display` already use
  (the control registry), so the map, the room-zoom, and the PWA agree. Keep it reload-aware (ADR-0027:
  area/state authoritative from the control registry).
- Unit-test: a running actuator with a status, an idle one, and one missing from the control registry
  (→ no `state`, panel falls back gracefully).

## Verification (arc 2)
- Running actuator → green row with the play glyph + its target/level; idle → dim with the pause glyph.
- An out-of-range actuator is red (alert beats running/idle).
- No `state` yet → actuator shows its metric as before (no regression).
