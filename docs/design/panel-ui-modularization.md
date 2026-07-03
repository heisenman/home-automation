# D1001 panel UI modularization — `ui_tiles.c` (1393 L) → `main/ui/` modules

**Date:** 2026-07-02  **Status:** PLAN (implement next session, after compact). **Owner:** ops.
Executes **ADR-0020** (module-first) on the panel; foundation for the panel's shared-ui-spec controls
renderer (pairs with `shared-ui-spec`). Behavior-preserving refactor of *working* code (v52-event).

> **Why.** `main/ui_tiles.c` is a 1393-line monolith holding every panel concern (grid, expand, charts,
> scenes, admin, actuator controls, HTTP, formatting, orchestration). It's the biggest un-modularized thing
> in the codebase and the prerequisite for (a) rendering the panel's actuator controls from the server
> `vm.controls` spec (the panel half of shared-ui-spec Phase 0 — the PWA half shipped in `0de4848`), and
> (b) future panel features. This is the **first execution of the module-first rule** (ADR-0020); do it
> exemplarily.

## Source-of-truth note (READ FIRST)
Panel firmware is **off-git** at `~/reterminal-dev/d1001-beachhead/` (dev tree, currently v52-event,
uncommitted). Repo mirror = `provisioning/reterminal/beachhead/` (synced through earlier builds). **Refactor
in the dev tree, build+flash+verify, THEN sync the mirror.** Do NOT edit the mirror directly.

## Guiding constraints (must not regress)
1. **Behavior-preserving.** The panel must render + behave identically to v52-event after each step: grid,
   inline-expand, 72h charts, scene selector, admin lock/unlock + keyboard, actuator command overlay,
   override + policy editor, battery indicator, °F fold, ascii-fold.
2. **Queue+worker discipline (the v11/v17 lesson).** ALL LVGL mutation + ALL blocking HTTP stay OFF the
   mqtt-callback and touch-click stacks. Every existing worker task (`ui_task`, `state_task`, `chart_worker`,
   `cmd_worker`, `admin_worker`) keeps its task; cross-module calls that touch `lv_obj` take the LVGL lock.
   The split moves code between files — it must NOT move work between stacks.
3. **Incremental + bisectable.** Extract ONE module at a time, `idf.py build` green after each, flash+eyeball
   at milestones. A broken step is then localized to the last extraction.

## Shared-static dependency map (what drives the boundaries)
The 34 file-scope statics partition cleanly by concern (verified against the code):

| Concern | Statics | Key functions (line@v52) |
|---|---|---|
| **transport** | `s_base` | `http_get` 181, `http_post_cmd` 483, `http_send_json` 603, `api_token_hex` 639 |
| **format/catalog** | `s_cat[]`, `s_ncat` | `ascii_fold` 133, `parse_catalog` 153, `is_celsius`/`disp_val`/`disp_unit` 176, `metric_of` 205, `parse_hex_color` 213 |
| **grid** | `s_grid`, `s_cards[]`, `s_ncards` | `card_for` 362, `card_clicked_cb` 354, `apply_state` 1004 |
| **chart** | `s_chart_q`, `CHART_SCALE` | `chart_fill` 523, `chart_worker` 552 |
| **expand** | `s_expbox`, `s_exp[]` | `expand_free` 227, `expand_close_cb` 240, `expand_open` 248 |
| **controls** | `s_cmd_q`, `s_acts[]`, `s_nacts`, `s_cmd_ov/title/result`, `s_cmd_target`, `s_admin_ctrls`, `s_ov_*`, `s_edit_*`, `s_ov_has_policy` | `cmd_worker` 497, `cmd_send_switch` 792, `ov_send_override` 808, `ov_*` 819–834, `act_clicked_cb` 846, `actuator_card` 872 |
| **scenes** | `s_topbar`, `s_scenebox`, `s_scene_btn[]`, `s_scene_name[]`, `s_nscenes`, `s_scene_active` | `render_house` 762, `scene_btn_cb` 721 |
| **admin** | `s_admin_btn/lbl/tok/active/last_us`, `s_admin_q`, `s_kb_*`, `s_toast` | `admin_worker` 666, `admin_paint_ui` 649, `admin_relock` 659, `kb_event_cb` 733, `admin_btn_cb` 748, `toast` 631 |
| **topbar-battery** | `s_batt_lbl` | `ui_tiles_set_battery` 1042 |
| **orchestration** | `s_url`, `s_disp_url`, `s_house_url`, `s_state_q`, `s_started`, `s_header` | `render` 949, `ui_task` 966, `state_task` 1030, `ui_tiles_start` 1081, `ui_tiles_on_state` 1073 |

Cross-concern coupling is small and one-directional: `expand`→`chart`+`format`+`http`; `grid`→`format`;
`controls`→`http`+`admin`(token); `scenes`→`http`+`admin`; `render`(orchestration)→ everyone. This is why
the split is tractable.

## Target layout — `main/ui/`
```
main/ui.h            public API (UNCHANGED contract: ui_tiles_start / ui_tiles_on_state / ui_tiles_set_battery)
main/ui/ui_internal.h  shared contract: extern accessors + cross-module fwd decls + the LVGL-lock convention
main/ui/ui_http.c/.h   http_get/post/send_json, api_token_hex, s_base derivation           [transport]
main/ui/ui_format.c/.h metric catalog + ascii_fold + disp_val/unit + metric_of + hex_color [presentation]
main/ui/ui_grid.c/.h   sensor grid + cards + apply_state                                    [grid]
main/ui/ui_chart.c/.h  chart_fill + chart_worker (72h)                                       [chart]
main/ui/ui_expand.c/.h inline expand panels (uses chart+format+http)                         [expand]
main/ui/ui_controls.c/.h actuator overlay + override + policy editor                         [controls]
main/ui/ui_scenes.c/.h scene selector + render_house                                         [scenes]
main/ui/ui_admin.c/.h  admin lock/unlock + keyboard + toast                                  [admin]
main/ui_tiles.c        SLIM composition root: render/ui_task/state_task/ui_tiles_start + wiring
```
Keep it in `main/ui/` for this pass (single build, lowest risk). Promotion to `firmware/components/ha_panel_ui`
is a LATER ADR-0020 step — note it, don't do it now.

### `ui_internal.h` — the crux
Rather than scatter `extern` statics, expose each module through a tiny init + a few accessors:
- `ui_http_init(const char *base)`, `char *ui_http_get(const char*, int*)`, `int ui_http_send(...)`.
- `ui_format_load_catalog(cJSON*)`, `double ui_disp_val(...)`, `const char *ui_disp_unit(...)`.
- `ui_admin_is_active()`, `ui_admin_touch()`, `void ui_admin_token(char*out)` (controls/scenes need the JWT).
- `ui_chart_request(int slot)` (expand→chart handoff via the existing queue).
- `lv_obj_t *ui_topbar(void)` / `ui_grid_container(void)` / `ui_expbox(void)` for the root to hand parents in.
Each module owns its statics privately; the header is the only cross-module surface. This is the module-first
pattern ADR-0020 wants to make loud.

## Extraction order (each step: build green; ★ = flash+eyeball milestone)
Leaf-first, so every step compiles against already-extracted modules:
1. **ui_format** (no deps) — move catalog+fold+disp helpers. Build.
2. **ui_http** (no deps) — move transport + `s_base`. Build.
3. **ui_chart** (deps: format) — move chart worker+fill. Build.
4. **ui_grid** (deps: format, http) — move grid+cards+apply_state. Build. ★ flash: grid + live state identical.
5. **ui_expand** (deps: chart, format, http) — move expand panels. Build. ★ flash: tap→expand+72h chart identical.
6. **ui_admin** (deps: http) — move admin+keyboard+toast. Build.
7. **ui_scenes** (deps: http, admin) — move scene selector+render_house. Build. ★ flash: scenes + lock/unlock.
8. **ui_controls** (deps: http, admin) — move actuator overlay+override+policy editor **verbatim**. Build.
   ★ flash: actuator command + override + policy edit identical.
9. **ui_tiles.c slim** — what remains is orchestration (`render`/`ui_task`/`state_task`/`ui_tiles_start`);
   convert its direct static pokes into the module init/accessor calls. Build. ★ full-panel regression eyeball.
10. **CMakeLists.txt** — add `ui/*.c` to SRCS (or `idf_component` glob); confirm one clean build.
11. **Sync the repo mirror** `provisioning/reterminal/beachhead/` + commit (module-first exemplar) + push.

## Phase 2 (SEPARATE, after the pure refactor verifies) — panel controls from `vm.controls`
`ui_controls` currently renders actuator controls from `vm.traits` (the old pattern the PWA just dropped).
Once the pure move is verified, migrate `actuator_card`/overlay to iterate the server `vm.controls` spec
(same contract the PWA now uses: kind∈override|setpoint|ranged|indicator, ranges/options/admin/action from
the BFF). This closes the **panel half of shared-ui-spec Phase 0**. Keep it a DISTINCT commit — never mix a
move with a behavior change. (Requires the panel to fetch `/api/v1/displays`; it already fetches `s_disp_url`.)

## Verification per milestone
Build green + `d1001-beachhead/#` bus sane + flash (USB — OTA can wedge a battery-backed panel; USB recovers)
+ eyeball the milestone feature. Heap flat across expand open/close (no leak). `idf.py monitor` for panics =
Hugh. Final: full regression pass (every feature above) before syncing the mirror.

## Risks / watchouts
- **LVGL lock**: any accessor that mutates `lv_obj` from another module's task must hold the lock exactly as
  today. Grep every `lv_*` call site during extraction; don't drop a `lv_obj` mutation onto the mqtt/click stack.
- **Queue ownership**: `s_chart_q`/`s_cmd_q`/`s_admin_q`/`s_state_q` — the creating module owns the queue; the
  producer side calls an accessor (`ui_chart_request`) rather than touching the handle.
- **api_token_hex / admin JWT**: shared by controls+scenes+admin → lives in `ui_admin`, exposed via accessor.
- **Off-git drift**: dev tree is the source; sync the mirror only at the end, in one commit.
- **Scope creep**: resist folding Phase 2 (vm.controls) into the move. Pure refactor first, verified, THEN migrate.

## Governance tie-in (ADR-0020 open action)
This refactor is the module-first exemplar. Alongside step 11: move **ADR-0020 → Accepted**, and add the loud
module-first rule to `firmware/AGENTS.md` (Hugh's open action item) citing this split as the reference.

## Estimated shape
~10 new files, ~1400 lines moved (not rewritten), one slim orchestrator. Multi-hour; the milestones (★) are
natural checkpoint/commit points. Start at step 1 after compact.
