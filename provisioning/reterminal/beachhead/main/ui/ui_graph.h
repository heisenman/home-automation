// D1001 graph builder — a top-level "Graphs" view (docs/design/d1001-graph-builder.md).
// A shared range control (6h/24h/7d/30d) over a stack of user-composed graph panels.
//
// Phase 2a (this file): scaffold — the view container + range control only. Panels, the trace
// picker (lv_dropdown), the multi-series overlay chart, and the fetch worker land in P2b+.
#pragma once
#include "lvgl.h"

// (Re)build the Graphs view into `parent` (called on nav-in, like ui_devices_render). Idempotent:
// clears `parent` and rebuilds its children. Caller holds the LVGL lock.
void ui_graph_render(lv_obj_t *parent);

// Currently-selected range, in hours (driven by the range control). Consumed by the fetch worker in
// P2b+ (maps onto the ha_replica rung horizon / the readings `hours` query).
int ui_graph_hours(void);
