// 72h chart fetch worker for the inline expansion panels — extracted from ui_tiles.c
// (ADR-0020 module-first). Owns the fetch queue + worker; reads/writes the expansion
// registry (ui_expand.h) under the LVGL lock. Deps: ui_expand, ui_http, ui_format.
#pragma once
#include <stdint.h>

// The 72h graph window + resolution (shared with ui_expand, which sizes the charts).
// Restored to 200 (was temporarily 100 to survive the old 64KB internal LVGL pool): the LVGL
// heap now lives in PSRAM (lv_mem_psram.c), so per-chart series memory is no longer the ceiling.
#define GRAPH_HOURS 72
#define GRAPH_POINTS 200

// Start the chart fetch worker + its queue. Called once by the orchestrator (ui_tiles_start).
void ui_chart_start(void);

// Enqueue a 72h chart fetch for expansion `slot` (epoch guards a close/reuse race). No-op if the
// worker isn't up yet. Called from the LVGL/click ctx (expand_open) — enqueue only, no HTTP here.
void ui_chart_request(int slot, uint32_t epoch);
