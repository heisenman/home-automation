// Graph time axis (Catherine, 2026-10-30) — the PWA AdaptiveChart's x-axis rules, for the D1001 Graphs
// view (docs/design/d1001-graph-builder.md). Local-time hour boundaries in the window: midnights are day
// grid lines, the others minor hour ticks (only for windows up to 7 days). Labels sit under the day lines
// ("10/28"); a window crossing fewer than 2 midnights also labels hour ticks ("14:00") every 1/2/3/6/12 h,
// the finest step that keeps about 7; then thinned to at most 7 so they don't collide.
#pragma once
#include <stdbool.h>
#include <time.h>
#include "lvgl.h"

// Draw the window [t0, t1] (epoch seconds): grid lines + hour ticks into `layer` (a transparent child
// covering the chart's plot area; cleaned first) and the labels into `xrow` (a no-layout strip the same
// width as the plot, under it; cleaned first). Caller holds the LVGL lock.
void ui_graph_axis_draw(lv_obj_t *layer, lv_obj_t *xrow, time_t t0, time_t t1);
