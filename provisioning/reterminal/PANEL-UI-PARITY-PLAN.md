# D1001 ⇄ PWA: shared-spec UI merge + panel web-app parity (inline expand, 72h charts, full controls, scenes, admin lock)

> ## ✅ SUPERSEDED / DELIVERED (2026-07-02) — historical plan, kept for rationale
> Every feature in this plan shipped and is HW-verified on the D1001 @ `.8`:
> - **Phase 0 (merge foundation)** — BFF `vm.controls`/`vm.display` as the single UI-truth; PWA renders it
>   (`0de4848`), and the **panel** now renders it too (Phase 2, `104411c`/v55).
> - **Phase A (reads)** — inline expand + stacked 72h `lv_chart`s on a scrollable screen (shipped in the
>   v17-era monolith; then charts moved to the **local SD rung replica**, `5b9d815`/v57).
> - **Phase B (top bar)** — scene selector + admin lock/unlock (LVGL keyboard → RAM admin JWT).
> - **Phase C (controls)** — override + setpoint/ranged/indicator + automation editor, all server-authored.
>
> The renderer was then **modularized** (`ui_tiles.c` 1393→206 L + 8 `ui/` modules,
> [docs/design/panel-ui-modularization.md](../../docs/design/panel-ui-modularization.md)) and the memory
> ceiling fixed (LVGL heap → PSRAM). The panel is now a thin renderer of the shared spec — the merge
> decision below was realized. **Do not re-implement from this plan;** it's kept for the merge rationale.
> Remaining descendant work lives under board items `shared-ui-spec` (open nits) and `ha-replica-panel`
> (Phase 2 incremental sync).

## Context
The D1001 wall panel (Phase 2 LIVE, v17) has native LVGL tiles with a **modal** tap-detail and basic on/off
via a scoped operator token. Hugh wants panel↔PWA **feature parity** (tap a tile → it expands INLINE below
the grid with present state + a **72h chart**; **multiple** stacked expansions with **Close** buttons on a
**scrollable** screen; actuators expose the PWA's **full controls incl. automation-parameter editing**; a top
bar with Home/Away/Sleep **scene icons**; **admin lock/unlock**). He also wants — where sensible — to **merge
UI development into one place** rather than maintain two divergent UIs. Plan now; **implement after a
checkpoint + compact** (context is low).

## Decisions (confirmed)
- **Hybrid / converge**, and **locked = view + basic device toggle** (keep `PANEL_TOKEN`; admin-unlock only
  for override/automation/scene edits). Graph window = **72h**.

## THE MERGE DECISION (Hugh's core question: is merging web-app vs MCU-display UI reasonable?)
**Yes — but at the SEMANTIC layer (the BFF), not the renderer.** A browser can't run on the ESP32‑P4 and LVGL
can't run in a browser, so the two **renderers must stay separate** (DOM widgets vs `lv_obj`). What *should*
merge is everything **above** rendering — the UI *decisions* currently duplicated/hardcoded in each surface.
Make the **BFF the single source of UI truth**; both the PWA and the panel become **thin renderers** of a
shared, server-authored spec. This is realistic and high-value; a full declarative manifest (server emits an
explicit widget tree both interpret) is *possible* but has **diminishing returns for two surfaces** and fights
rich forms (policy editor, keyboards) — reserve it for the generalizable parts only, not the whole UI.

**Merge boundary (what moves to the BFF vs stays native):**
- **MERGE → BFF view-models (`server/api/viewmodel.py`, extend `build_sensor_list`/`build_display`):** which
  metrics to show + **graph** and their unit/color/precision/normal-range (today hardcoded in app.js
  `metricGraphs` L612); **trait→control** mapping + ranges/steps/labels/admin-flag per actuator (today
  app.js maps traits→widgets); scene list + icons + which are editable (partly in `/api/v1/house`);
  **validation constraints** (min/max, ON>OFF) surfaced so both clients pre-validate identically (server
  already enforces authoritatively); each control's **action contract** (endpoint + payload). Net: ~the "what
  + rules" (the bulk of UI logic decisions) lives once.
- **STAY native (cannot/should-not merge):** actual widget construction, input methods (HTML inputs vs LVGL
  keyboard/rollers/steppers), scroll/layout mechanics, event loop, styling. Two **thin** renderers remain.

**Why this dissolves the "refactor vs rebuild" tension:** the panel-parity work and the merge are the **same
work** if done **spec-first** — for each feature, extend the BFF spec, refactor the PWA to consume it, and
render the panel from it. The panel is not a second UI codebase; it's a second thin renderer of the BFF spec,
and the PWA becomes the same. Nothing is thrown away; the PWA is refactored in place and verified against
itself.

## THE MERGE PROCESS (incremental, low-risk, spec-first)
1. **Define a versioned shared UI-spec** as additive fields on the existing BFF view-models (NOT a new
   endpoint) — start minimal: a `display` spec for sensors (metrics→graph + unit/color/range) and a
   `controls` spec for actuators (control list + ranges + admin flag + action contract). ADR it (extend
   ADR-0019 or a new "shared UI spec" ADR). Add unit tests on the spec in `server/`.
2. **Refactor the PWA to consume the spec** for the covered parts (drop hardcoded `metricGraphs` / trait
   mapping; render from server fields). Verify **no behavior change** against the live PWA.
3. **Build the panel renderer against the SAME spec** (generic: given the spec, lay out known LVGL widget
   types).
4. **Iterate feature-by-feature** (tiles → charts → controls → scenes), each spec-first: extend BFF → point
   BOTH renderers at it.
5. **Keep bespoke interactions per-platform** (rich policy editor, on-screen keyboard) but driven by shared
   constraints; generalize later only if it pays off.
6. **Governance:** BFF view-model = single UI-truth source; one change updates both surfaces. No panel-specific
   server endpoints (already the case).

## Parity implementation — phased (each phase = spec-first + independently OTA-able, rollback-safe)
Refactor `main/ui_tiles.c` into `main/ui/` units (`ui_grid`, `ui_expand`, `ui_chart`, `ui_controls`,
`ui_scenes`, `ui_admin`, `ui.h`), preserving the **queue+worker** discipline (all LVGL + blocking HTTP off the
mqtt-callback / touch-click stacks — the v11/v17 lesson).

- **Quick win (standalone, do first): back button toggles the screen.** The physical button is
  **GPIO 3** (`BSP_BUTTON_IN`, via the `espressif/button` component / `button_gpio.h`). On short-press,
  toggle the screen on/off — reuse `bsp_display_off()` + add `bsp_display_wake()` (expander `BL_EN`/`PWR_EN`
  back on + `esp_lcd_panel_disp_on_off(true)` + restore backlight; NO re-init, since LVGL is already up →
  instant). Track on/off state; debounce via the button component. Small, self-contained, ships before the
  parity work.
- **Phase 0 (BFF spec seed):** add display+controls spec fields to `build_sensor_list`/`build_display`;
  refactor the PWA to read them (verify unchanged). *This is the merge foundation.*
- **Phase A (reads, no auth):** modal → **inline expand**. Root = one vertical **scrollable** container:
  `[top bar]→[tile grid]→[stack of expanded panels]`. Tap → append full-width panel below (multiple), each
  with **Close**; stack downward; scroll. Panel = present state + **`lv_chart`** per spec-listed metric from
  `GET /devices/{id}/readings?start&end&metric&limit` (72h, server-downsampled). New `ui/ui_chart.c` (series
  on a worker → chart under the LVGL lock).
- **Phase B (top bar):** scene icons 🏠/🚪/🌙 from `/api/v1/house`, active highlighted, tap → `POST
  /control/house/scene` (admin). Lock/unlock: 🔒→**LVGL keyboard** password → `POST /auth/login` → admin JWT
  in RAM (`s_admin_tok` + idle auto-lock) → 🔓; lock/timeout discards it. Mirrors `AdminModal`/`lock()`.
- **Phase C (actuator controls, admin-gated, shown only when unlocked):** manual (switchable [have] + ranged
  steps + setpoint stepper), override (Boost/clear → `/control/{id}/override`), automation editor (strategy,
  `on_above`/`off_below` steppers, Away/Sleep scene profiles, quiet window → `PUT /control/{id}/policy`),
  driven by the Phase-0 spec + server validation (don't reimplement rules).

## PWA IS the reference (server contracts — reuse verbatim; `server/web/app.js`)
Reads `/api/v1/{sensors,displays,house}`; history `GET /devices/{id}/readings` (+ `/weather/readings`); writes
`POST /control/{id}/override`, `PUT /control/{id}/policy`, `POST /control/house/scene`; auth `POST /auth/login`;
commands `POST /devices/{id}/command` (operator, wired). Ref components: `ExpandedSensor` L658,
`fetchReadingsRange` L127, `metricGraphs` L612, `OverrideControls` L181, policy `Settings` L224, manual L384,
purifier automation editor L463, `SCENE_ICON`/scene-sel L1091, `AdminModal`/`lock` L1036/L1209.

## Critical files
- Server (merge): `server/api/viewmodel.py` (spec fields) + tests; `server/web/app.js` (refactor to spec).
- Panel: `main/ui_tiles.c` → `main/ui/*.c` + `ui.h`; new `ui_chart.c`, `ui_admin.c`, `ui_scenes.c`,
  `ui_controls.c`; HTTP helpers gain GET-range + admin-Bearer POST; `main/CMakeLists.txt`.
- `main/beachhead_main.c` / `secrets.h` — unchanged (`PANEL_TOKEN`+`BFF_BASE_URL` present).

## Verification (per phase: build→OTA→bus `d1001-beachhead/#`; `idf.py monitor` = Hugh for panics)
- **0:** PWA behaves identically after reading spec from server (diff nothing visible); spec unit tests pass.
- **A:** tap → inline panels stack+scroll; 72h charts match the PWA; Close works; heap flat (no leak).
- **B:** scene icons reflect+set `/api/v1/house` (also visible in PWA); unlock→🔓; lock/idle discards; locked
  scene-set fails politely.
- **C:** unlocked → edit an automation param + set an override from the panel → confirm server-side
  (`/api/v1/displays`, control.db) AND in the PWA; locked → edit controls hidden, on/off still works.

## Checkpoint / resume (implement next session)
Board item `reterminal-panel` (ops) covers this; add a `shared-ui-spec` item for Phase 0 + ADR. Firmware base
= v17 (`98cb671`), device live at `.8` (OTA+rollback, `PANEL_TOKEN` operator; admin unlock mints its own JWT).
Recommended order: **Phase 0 (spec) → A → B → C**, each shippable. Scope is large (LVGL charts/keyboard/forms);
build incrementally, checkpoint each phase.
