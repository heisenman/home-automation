// Inline expand-below panel registry — the shared surface between ui_expand (owner) and
// ui_chart (fills the charts). Extracted from ui_tiles.c (ADR-0020 module-first).
//
// The expansion registry is a deliberately shared internal contract (the ui_internal.h role):
// ui_expand builds/tears down the panels and owns s_exp[]; ui_chart's fetch worker reads a
// slot's target and writes its charts, both under the LVGL lock with an epoch re-check so a
// close mid-fetch discards the result safely. Until step 5 the definitions live in ui_tiles.c;
// s_exp[] then moves to ui_expand.c (the extern here is unchanged).
#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "lvgl.h"
#include "ui/ui_format.h"   // struct gspec, MAX_GRAPHS

// One inline expansion panel: a device's present state + one 72h chart per graphable metric.
struct expand_ref {
    char id[40];
    char name[40];
    bool active;
    uint32_t epoch;                // bumped on close/reuse; the fetch worker compares to avoid stale writes
    lv_obj_t *panel;
    lv_obj_t *chart[MAX_GRAPHS];
    lv_chart_series_t *ser[MAX_GRAPHS];
    lv_obj_t *note[MAX_GRAPHS];    // per-chart "loading…/no data" label
    struct gspec g[MAX_GRAPHS];
    int ngraph;
};
// Concurrent inline expansions before the oldest slot is recycled (a tap beyond this closes the
// 1st). Restored to 6 (was temporarily 3 to survive the old 64KB internal LVGL pool): the LVGL
// heap is now in PSRAM (lv_mem_psram.c), so this cap is a UX/scroll-length choice, not a memory
// limit. expand_open still recycles beyond MAX_EXPAND, so raising it further is safe.
#define MAX_EXPAND 6
extern struct expand_ref s_exp[MAX_EXPAND];   // the live expansion registry (defined by the owner)

// Seed for opening an expansion: the tapped card's identity + present-state + graph specs.
// ui_grid (owner of the card registry) fills this and calls expand_open, so ui_expand needs
// no knowledge of the grid module. Pointers borrow the caller's card_ref; used only during
// the (synchronous, LVGL-locked) expand_open call.
struct expand_seed {
    const char *id;
    const char *name;
    const char *room;
    const char *detail;          // present-state multiline (may be NULL/empty)
    const struct gspec *g;       // graphable metrics (ngraph entries)
    int ngraph;
};

// Create the expansion container (the vertical box below the grid) under `parent`. Called once
// by the orchestrator while it holds the LVGL lock, at the point it builds the screen.
void ui_expand_init(lv_obj_t *parent);

// Open (or toggle-close) an inline expansion panel for the seeded device + enqueue its 72h
// fetch. Runs in the LVGL/click ctx (card tap).
void expand_open(const struct expand_seed *seed);
