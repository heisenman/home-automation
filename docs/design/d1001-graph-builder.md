# D1001 graph builder — design log (full free-form parity)

Status: **DESIGN** (running log → ADR on acceptance). Owner: dev. Target device: D1001 panel
(`provisioning/reterminal/beachhead`, ESP32-P4, LVGL). Decision to pursue **full free-form parity**
with the PWA builder was Hugh's call (2026-07-13), over a simplified fixed-panel version.

## 1. Goal / parity target

Bring the PWA's **graph builder** (`server/web/app.js` `GraphBuilder`/`Panel`/`AdaptiveChart`/
`RangeControl`/`traceCatalog`) to the panel render. Full parity means:

- **Trace catalog** = every sensor metric (each sensor × its present `GRAPHABLE` metrics) **plus**
  every weather metric (`/weather/meta` locations × metrics). ~100–150 traces typical.
- **N panels**, add/remove. Each panel holds a user-chosen set of traces.
- **Per-panel trace picking** — add via a picker, remove via a chip ✕.
- **Multi-trace overlay per panel** with mixed-unit handling: same unit → shared real-value scale;
  mixed units → each trace normalized to its own 0–100 % range (trend comparison), real ranges shown
  in the legend. (`AdaptiveChart` semantics.)
- **Shared range control** — presets 6h/24h/7d/30d + custom window.
- **Legend** with per-trace color + (mixed-unit) real range.

**Not in the parity target: the PWA's per-point hover readout** (2026-08-06 — crosshair + tooltip
reading every trace out at the nearest sample). It's a *pointer* affordance and the D1001 is a touch
panel with no hover state, so porting it would mean inventing a different interaction (tap-to-inspect),
not matching one. Worth noting because it carries real information on the PWA side: in mixed-unit
**normalized** mode the readout is the only place the real values appear per-point, and the panel's
equivalent is still the legend's per-trace range. If a panel tap-to-inspect is ever wanted, treat it as
its own decision rather than parity work.

## 2. What already exists (build ON this, don't reinvent)

- **Chart data plane** — `ui/ui_chart.c` `chart_worker`: off the LVGL/click stack, **local-SD rung
  replica first** (`ha_replica_rung_query(id, key, hours, …)`) → **HTTP fallback**
  (`/devices/{id}/readings?metric=&hours=&limit=`), fills `lv_chart` series under the LVGL lock with an
  **epoch re-check** so a close/reuse mid-fetch is discarded safely. Int fixed-point (`CHART_SCALE=100`),
  `disp_val` for °C→°F. This is the exact pattern the builder's fetch reuses — just parameterized by
  (panel, trace, hours) instead of (expansion slot, its graphs, fixed 72h).
- **HTTP transport** — `ui/ui_http.c` `ui_http_get(url,&len)` (malloc'd buf, caller frees) + `ui_http_base()`.
- **lv_chart multi-series** — `lv_chart_add_series` already supports N series on one chart (the expand view
  uses one-series-per-chart; the builder overlays N-series-per-chart).
- **Nav model** — `ui_tiles.c`: landing = house map (`/api/v1/rooms`) ↔ room-zoom ↔ a **"Devices" view**
  (`s_devview`), toggled by a **title-row button** (`s_devbtn`) with a **"‹ Rooms" back button** (`s_back`),
  re-render driven by `s_nav_dirty`. The builder mounts as a **new peer view** the exact same way.
- **AQ/format helpers** — `ui/ui_format.c` catalog (`GRAPHABLE` order, `disp_val`, `parse_hex_color`).
- **Weather** — server already serves `/weather/meta` (`available`, `metrics`) and
  `/weather/readings?metric=&start=&end=&limit=`.

## 3. Architecture (LVGL translation of the PWA)

New module `ui/ui_graph.c/.h` (ADR-0020 module-first), mounted as a top-level view.

| PWA (JS/HTM/SVG) | Panel (LVGL/C) | Notes |
|---|---|---|
| `traceCatalog()` | build once per render from the cached `/api/v1/sensors` + `/weather/meta` | array of `struct trace {char key,label,unit,source,metric; uint8_t kind;}` — **capped** (see §4) |
| `<select>+ add trace…` | **`lv_dropdown`** populated with catalog labels, filtered to unchosen | native LVGL widget → direct map; scrollable, touch-friendly |
| trace chip `label ✕` | `lv_button` (label + ✕), click → remove from panel | reuse chip look from `ui_grid`/map |
| `RangeControl` presets | row of `lv_button` (6h/24h/7d/30d) | maps to `hours` for the fetch; **custom datetime deferred** (no native LVGL date input — see §5) |
| `AdaptiveChart` overlay | one `lv_chart`, N series; per-series min/max; shared-scale vs per-series-normalized | port the normalization math verbatim; legend = colored labels + real range |
| `Panel` (traces + plot + fetch on change) | a container: chart + chips + dropdown + remove btn; enqueue fetch on trace/range change | fetch via the extended chart worker |
| `GraphBuilder` (N panels + range + add) | the view root: RangeControl + panel list + "+ Add graph" | state model in §4 |

### Data / concurrency
- Extend the chart worker to a **generic (target, trace, hours) fetch queue** — or add a parallel
  `ui_graph` worker with the same shape. **One worker, sequential** fetches (blocking HTTP) with a
  per-panel "loading…" note. The PWA fetches traces concurrently (async); the panel does **sequential**
  — acceptable for v1; note it as a known latency gap, revisit only if painful. Epoch/generation guard
  identical to `ui_chart` so a nav-away or trace-change mid-fetch is discarded.
- Reuse **local-replica-first**: `ha_replica_rung_query` already resolution-selects by `hours`, so
  presets 6h→720h ride the existing rung ladder; HTTP fallback for weather (no SD replica) + cold start.

## 4. Memory model + hard guardrails (the real constraint, not flash)

Headroom is fine on paper (8 MB PSRAM @200MHz, ~6–7 MB free; app partition 45 % free). The risk is
**unbounded** builder state competing with the LVGL heap when the user opens many panels/traces while a
detail overlay + admin keyboard are also live. So the design **caps** it:

- `MAX_PANELS` = 4, `MAX_TRACES_PER_PANEL` = 6, `GRAPH_POINTS` = 200 (existing).
- Per series: 200 × 4 B ≈ 0.8 KB + `lv_chart` series overhead. Worst case 4×6 = 24 series ≈ ~20 KB
  data + chart objects ≈ well under 100 KB. Trace catalog ~150 × ~120 B ≈ 18 KB.
- All chart/series buffers from **PSRAM** (LVGL allocator already routes there via `lv_mem_psram`).
- **`log()` the cap** when hit ("panel limit reached") — never silently drop (no-silent-caps rule).
- Free all panel/trace/series state on nav-away (mirror `ui_expand_clear`).

## 5. Touch UX considerations (the heavy part, per recon)

- **Trace picker** via `lv_dropdown` is the make-or-break: 150 options is a long scroll. Mitigation:
  group/prefix labels by source (`kitchen · Temp`, `weather KSQL · …`) as the PWA does; consider a
  future filter box. v1 = plain scroll (ship, then refine).
- **Custom datetime**: LVGL has no date input. v1 ships **presets only**; custom range is a follow-up
  (either an `lv_roller` span picker or "last N" stepper). Documented gap, not silent.
- Wall-panel ergonomics: a free-form builder is dense for a wall device. Keep panels stacked in a
  scrollable column; one chart ~150 px tall like the expand view. This is a "kiosk explore" surface,
  not a primary glance view — reached via its own nav button, not the landing.

## 6. Rejected alternatives

- **Simplified fixed-panel builder** (preset Climate/AQ tabs) — rejected per Hugh's call for full parity.
  Kept in back pocket as the fallback if touch UX proves unworkable.
- **Server-rendered chart images streamed to the panel** — avoids LVGL charting entirely, but adds a
  server render path + image transport + kills local-replica-first offline charts. Rejected: the panel
  already charts well locally.
- **Parallel multi-fetch worker pool** — rejected for v1 (complexity); sequential worker is enough at
  these trace counts. Revisit if latency is felt.

## 7. Open questions (resolve during build)

- Where does the nav button live — title row next to "Devices", or the scene/top-bar? (lean: title row.)
- Is `GRAPH_POINTS=200` enough for a 30 d window, or bump for the widest range? (start at 200, measure.)
- Does `ha_replica_rung_query` cover all preset horizons incl. 30 d, or does 30 d always fall to HTTP?
  (verify the rung ladder's longest rung.)

## 8. Phasing (each phase ships + cable-flash-verifies independently)

- **P2a — scaffold + nav**: `ui_graph.c/.h`, new view + title-row button + back, empty "Graphs" screen,
  RangeControl (presets). Verify: navigable, range buttons toggle.
- **P2b — single panel, single trace**: dropdown picker (sensor traces only) + one `lv_chart` fed by the
  extended worker. Verify: pick a trace, it plots over the chosen range.
- **P2c — multi-trace overlay**: N series/panel, shared-scale vs normalized, legend with real ranges.
  Verify against the PWA on the same traces.
- **P2d — multi-panel**: add/remove panels, caps + `log()`. Verify memory flat across open/close cycles.
- **P2e — weather traces + polish**: weather catalog into the picker, label grouping, empty/loading/no-data
  states. (Custom datetime = separate follow-up.)

## 9. Estimate

Grounded in this session's actual pace (S3 fix + AQ tiles + AQ map each landed in well under a day
including build/flash/verify), and that ~70 % of the data plane already exists:

- P2a scaffold + nav — **~0.5 day**
- P2b single panel/trace (extend worker, dropdown) — **~1 day**
- P2c multi-trace overlay + normalization + legend — **~1 day**
- P2d multi-panel + caps + memory verification — **~0.5 day**
- P2e weather + polish states — **~0.5 day**

**Total ≈ 3.5 dev-days** for full free-form parity (custom-datetime picker excluded — separate ~0.5 day
follow-up). Each phase is independently cable-flash-verifiable, so it can land incrementally. (For
contrast, the earlier recon's "1–2 weeks" assumed building the chart/data plane from scratch — it's
already here.)
