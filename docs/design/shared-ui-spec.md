# Shared UI spec — the BFF as single UI-truth source (dev ⇄ ops coordination contract)

**Date:** 2026-07-02  **Status:** Working contract (living). Under ADR-0013 (presentation) + ADR-0019 (panel).
**Owners:** interface = shared; server impl = **dev** (proposed); renderers = **ops** (proposed) — see Division below.

> **Why this doc exists (coordination moment).** Both dev and ops appear to be converging on the same idea:
> make the server (BFF view-models) the single source of UI truth so the PWA and the D1001 panel stop
> hardcoding presentation client-side. This doc pins the **contract** both sides build to, so we can work in
> parallel without colliding on `server/api/viewmodel.py` / `server/web/app.js`. **If you (dev) are already
> building any of this, reply on board item `shared-ui-spec` and we'll carve ownership before either side
> edits shared files.**

## The decision (what merges, what doesn't)
Merge at the **semantic layer (the BFF)**, NOT the renderer. A browser can't run on the ESP32-P4 and LVGL
can't run in a browser, so the **two renderers stay separate** (DOM widgets vs `lv_obj`). What merges is
everything *above* rendering — the UI *decisions* currently duplicated in each surface:

- **`server/api/viewmodel.py` = the single source of UI truth.**
- **PWA (`server/web/app.js`) and the D1001 LVGL panel = thin renderers** of the server-authored spec.
- One change to the spec updates both surfaces. No panel-specific server endpoints (already the case).

## Current state (as of 2026-07-02)
| Piece | State | Where |
|---|---|---|
| **Display spec** (metrics → graph + label/unit/color/precision/order) | ✅ DONE, both consume it | `viewmodel.py` `METRIC_CATALOG` / `sensor_graphs` / `ui_metric_catalog`; panel folded it in (`5ff2f93`); PWA falls back to baked `GRAPHABLE` only if absent |
| **Controls spec** (actuator trait → control widget + ranges/steps/labels/admin-flag/action-contract) | ❌ NOT done — still client-side | `viewmodel.py` `build_display` emits **raw `traits`**; `app.js` `ManualControl` (L380) + `OverrideControls` (L181) map traits→widgets |
| PWA refactor onto controls spec | ❌ pending | `app.js` |
| Panel renderer for controls | ❌ pending | D1001 `main/ui/` (post ui_tiles split) |

So the display half of the merge is proven end-to-end. **This doc is mostly about the controls half + the
panel renderer.**

## THE CONTRACT — `controls` on the display view-model
`build_display(...)` gains a server-authored, ordered **`controls`** list (alongside today's raw `traits`,
kept for back-compat during migration). Each entry is a fully-specified, render-ready control descriptor.
Both renderers iterate `controls` and switch on `kind`; neither re-derives ranges, labels, admin-gating, or
the action contract.

```jsonc
// vm.controls: ordered list. Renderers render in order, switch on `kind`.
[
  // 1) power override — a CONTROL-LEVEL action (not a device trait)
  {
    "kind": "override",
    "label": "Power",
    "admin": true,                       // hidden/disabled when the client is not admin-unlocked
    "action": { "method": "POST", "path": "/control/{id}/override" },
    "presets": [
      { "action": "off",      "duration_min": 60, "label": "Off 1h" },
      { "action": "boost_on", "duration_min": 60, "label": "Boost 1h" },
      { "action": "clear",                          "label": "Resume auto" }  // shown only when override active
    ],
    // arbitrary duration alongside the presets: ONE number + ONE unit + an action button. The renderer
    // multiplies value * unit.mult into the SAME `duration_min` the presets post — one endpoint, one
    // validation path. `max_min` mirrors the API cap (server stays the authority; this is a courtesy
    // bound so the UI can grey the button instead of round-tripping a 400). Absent → presets only.
    "custom": {
      "label": "Off for",
      "units": [ { "key": "min", "label": "minutes", "mult": 1 },
                 { "key": "hour", "label": "hours", "mult": 60 },
                 { "key": "day",  "label": "days",  "mult": 1440 } ],
      "max_min": 10080,                   // = server/api/control.py MAX_OVERRIDE_MIN (7d)
      "default": { "value": 6, "unit": "hour" },
      "actions": [ { "action": "off", "label": "Off" } ]
    },
    "state": { "from": "override" }       // renderer reads vm.override for current state / minutes-left
  },

  // 2) setpoint — numeric input (e.g. dehumidifier target RH)
  {
    "kind": "setpoint", "trait": "setpoint",
    "label": "Target humidity", "unit": "%",
    "min": 40, "max": 60, "safe_value": 50, "admin": true,
    "action": { "method": "POST", "path": "/devices/{id}/command",
                "trait": "setpoint", "action": "set", "arg_key": "value" },
    "now_key": "target_pct"               // which vm.actuator field shows the live value
  },

  // 3) ranged — enumerated levels (e.g. fan Low/Med/High)
  {
    "kind": "ranged", "trait": "ranged",
    "label": "Fan speed", "admin": true,
    "options": [ { "value": 1, "label": "Low" }, { "value": 2, "label": "Med" }, { "value": 3, "label": "High" } ],
    "action": { "method": "POST", "path": "/devices/{id}/command",
                "trait": "ranged", "action": "set", "arg_key": "level" },
    "now_key": "fan_speed"
  },

  // 4) indicator — boolean on/off (e.g. panel LED)
  {
    "kind": "indicator", "trait": "indicator",
    "label": "LED / panel light", "admin": true,
    "action": { "method": "POST", "path": "/devices/{id}/command",
                "trait": "indicator", "action": "set", "arg_key": "on" },
    "now_key": "led_on"
  }
]
```

### Derivation rules (server side)
- `override` is emitted whenever the device has a control policy (every `build_display` device). `presets`
  are the fixed Off/Boost/Resume set the PWA uses today; `clear` preset is advisory-flagged for "only when
  an override is active" (renderer already has `vm.override`).
- `custom` (added 2026-08-02) is the arbitrary-duration entry. **A renderer may ignore it** — the PWA
  renders it; the D1001 panel currently does not (it has no numeric-entry widget, and `ui_controls.c`
  caps at `MAX_PRESET` 4 buttons), so the panel keeps preset-only pauses until that lands. Renderers must
  therefore treat `custom` as optional, not assume the presets are the whole story.
- `setpoint` / `ranged` / `indicator` are emitted **iff** the device's `traits_cfg` contains that trait.
  Pull `min`/`max`/`safe_value` (setpoint), `min`/`max`/`step`→enumerated `options` (ranged; the 2-level and
  3-level label maps `Low/High`, `Low/Med/High` move server-side), from `traits_cfg` (see
  `server/control/traits.py`).
- `admin: true` on all of them today (writes are admin-gated). Encoded per-control so a future view-only
  control needs no client change.
- `action` is the **exact** contract the renderer issues — method + path template (`{id}` substituted
  client-side) + trait/action/arg_key. This kills the last client-side knowledge of endpoint shapes.
- The ranges the server emits mirror what the server **already validates** (`traits.validate_command`), so
  both clients pre-validate identically and the server stays the authority.

### Ownership of the ranges the charts need (Phase A dependency)
The inline-expand + 72h chart (panel Phase A) reads `GET /devices/{id}/readings?start&end&metric&limit`
(server-downsampled). That endpoint **exists** (PWA uses it). No new server contract needed for charts beyond
the display spec already shipped — flagged here only so we don't double-build it.

## Proposed division of labor (dev confirms/counters on the board)
The spec lives in **server code**, which runs live on `.210` (dev's box; deploys are gated). To respect the
machine/domain split and avoid both of us editing `viewmodel.py`:

| Work | Proposed owner | Rationale |
|---|---|---|
| `controls` spec in `viewmodel.py` + `server/tests/test_viewmodel_controls.py` | **dev** | server = dev domain, live on `.210`, gated deploy |
| Refactor `app.js` onto `controls` (drop client trait→widget map; verify zero visible change) | **ops** | ops drove the display-spec fold; knows both renderers' needs |
| D1001 panel: `ui_tiles.c` (1393 L) → `main/ui/` modular split, then render `controls` | **ops** | panels = ops (bench); ADR-0020 module-first |
| This contract doc (keep it current as the interface evolves) | **shared** | single source of the interface |

**Alternative if dev is heads-down elsewhere:** ops drafts the `controls` function + tests as a reviewed
patch (pure, unit-testable on the bench), dev owns merging it live. Either way — **dev picks; ops holds all
edits to `viewmodel.py` until dev replies** so we don't collide.

## Phasing (each phase spec-first + independently shippable)
- **Phase 0** — this controls spec + PWA refactor (display half already done). *Foundation.*
- **Phase A** — panel modal → inline-expand + 72h charts (reads only).
- **Phase B** — panel top bar: scene icons + admin lock/unlock.
- **Phase C** — panel actuator controls (admin-gated), rendered from this `controls` spec.

## How we coordinate
- Board item **`shared-ui-spec`** (ops-claimed) is the ledger. **Contract lives here (this doc).**
- Collision zone = `server/api/viewmodel.py`, `server/web/app.js`. Whoever edits, claim the sub-work on the
  board first. ops is live-monitoring `ha/agents/#`.
- Dev: reply on `shared-ui-spec` with (a) whether you're already building the controls spec, (b) which row
  of the division table you take. Then we both build to the contract above, in parallel, no collision.
