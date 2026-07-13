// D1001 graph builder — a top-level "Graphs" view (docs/design/d1001-graph-builder.md).
// A shared range control (6h/24h/7d/30d) over user-composed graph panels.
//
// Phase 2b: range control + ONE panel with an lv_dropdown trace picker and one lv_chart, fed by a
// dedicated fetch worker (local-SD rung replica → BFF readings). Multi-trace overlay (P2c) and
// multi-panel (P2d) build on this.
#pragma once
#include "lvgl.h"
#include "cJSON.h"

// (Re)build the Graphs view into `parent` (called on nav-in). `sensors` = the /api/v1/sensors
// `sensors` array (the trace catalog is built from each sensor's server-authored `graphs`). May be
// NULL/absent (renders an empty catalog). Idempotent: clears + rebuilds `parent`. Caller holds the
// LVGL lock.
void ui_graph_render(cJSON *sensors, lv_obj_t *parent);

// Currently-selected range, in hours (driven by the range control). Consumed by the fetch worker.
int ui_graph_hours(void);

// Start the graph fetch worker + its queue. Called once by the orchestrator (ui_tiles_start),
// alongside ui_chart_start().
void ui_graph_start(void);
