// Inline expand-below panels (see ui_expand.h). Extracted from ui_tiles.c verbatim (ADR-0020
// module-first); behavior identical. Owns the expansion registry s_exp[] and the container
// s_expbox; ui_chart's worker fills the charts. Runs in the LVGL/click context (object
// creation is fine there; the 72h HTTP fetch is deferred to ui_chart's worker).
#include "ui/ui_expand.h"
#include "ui/ui_chart.h"   // ui_chart_request + GRAPH_POINTS
#include <string.h>
#include <stdio.h>
#include "lvgl.h"

struct expand_ref s_exp[MAX_EXPAND];   // the live expansion registry (extern in ui_expand.h)
static lv_obj_t *s_expbox;             // vertical container BELOW the grid holding expansion panels

void ui_expand_init(lv_obj_t *parent)
{
    s_expbox = lv_obj_create(parent);
    lv_obj_set_width(s_expbox, lv_pct(100));
    lv_obj_set_height(s_expbox, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(s_expbox, 0, 0);
    lv_obj_set_style_border_width(s_expbox, 0, 0);
    lv_obj_set_style_pad_all(s_expbox, 0, 0);
    lv_obj_set_style_pad_top(s_expbox, 10, 0);
    lv_obj_set_style_pad_row(s_expbox, 10, 0);
    lv_obj_set_flex_flow(s_expbox, LV_FLEX_FLOW_COLUMN);
    lv_obj_clear_flag(s_expbox, LV_OBJ_FLAG_SCROLLABLE);
}

// Close an expansion: delete its panel, bump epoch (so an in-flight fetch discards its
// result), mark the slot free. Caller MUST hold the LVGL lock.
static void expand_free(int slot)
{
    struct expand_ref *x = &s_exp[slot];
    if (!x->active) return;
    x->active = false;
    x->epoch++;
    if (x->panel) lv_obj_del(x->panel);   // deletes children (charts/notes) too
    x->panel = NULL;
    for (int i = 0; i < MAX_GRAPHS; i++) { x->chart[i] = NULL; x->ser[i] = NULL; x->note[i] = NULL; }
    x->ngraph = 0;
    x->id[0] = 0;
}

static void expand_close_cb(lv_event_t *e)
{
    int slot = (int)(intptr_t)lv_event_get_user_data(e);
    if (slot >= 0 && slot < MAX_EXPAND) expand_free(slot);
}

// Close EVERY open expansion — called on a nav change (leaving a room / house map) so inline charts
// don't pile up below the map. Persistent visualization belongs in the graph builder. LVGL lock held.
void ui_expand_clear(void)
{
    for (int i = 0; i < MAX_EXPAND; i++) expand_free(i);
}

// Build the expansion panel for the seeded device and enqueue its 72h chart fetch. Runs in the
// LVGL/click context (object creation is fine here; the HTTP fetch is deferred to a worker).
void expand_open(const struct expand_seed *seed)
{
    // toggle: if this device is already expanded, close it instead
    for (int i = 0; i < MAX_EXPAND; i++)
        if (s_exp[i].active && strcmp(s_exp[i].id, seed->id) == 0) { expand_free(i); return; }
    // find a free slot
    int slot = -1;
    for (int i = 0; i < MAX_EXPAND; i++) if (!s_exp[i].active) { slot = i; break; }
    if (slot < 0) { expand_free(0); slot = 0; }     // all full: recycle the oldest (slot 0)
    struct expand_ref *x = &s_exp[slot];
    memset(x->chart, 0, sizeof(x->chart));
    snprintf(x->id, sizeof(x->id), "%s", seed->id);
    snprintf(x->name, sizeof(x->name), "%s", seed->name[0] ? seed->name : seed->id);
    x->ngraph = seed->ngraph;
    for (int i = 0; i < seed->ngraph; i++) x->g[i] = seed->g[i];
    x->active = true;

    lv_obj_t *panel = lv_obj_create(s_expbox);
    x->panel = panel;
    lv_obj_set_width(panel, lv_pct(100));
    lv_obj_set_height(panel, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_color(panel, lv_color_hex(0x111834), 0);
    lv_obj_set_style_border_width(panel, 0, 0);
    lv_obj_set_style_radius(panel, 12, 0);
    lv_obj_set_style_pad_all(panel, 12, 0);
    lv_obj_set_flex_flow(panel, LV_FLEX_FLOW_COLUMN);
    lv_obj_clear_flag(panel, LV_OBJ_FLAG_SCROLLABLE);

    // header row: title (name · room)  +  Close (✕)
    lv_obj_t *hdr = lv_obj_create(panel);
    lv_obj_set_width(hdr, lv_pct(100));
    lv_obj_set_height(hdr, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(hdr, 0, 0);
    lv_obj_set_style_border_width(hdr, 0, 0);
    lv_obj_set_style_pad_all(hdr, 0, 0);
    lv_obj_set_flex_flow(hdr, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(hdr, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(hdr, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *ttl = lv_label_create(hdr);
    lv_label_set_text_fmt(ttl, "%s  -  %s", x->name, seed->room);
    lv_obj_set_style_text_font(ttl, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(ttl, lv_color_hex(0xffffff), 0);

    lv_obj_t *cls = lv_button_create(hdr);
    lv_obj_set_size(cls, 52, 40);
    lv_obj_set_style_bg_color(cls, lv_color_hex(0x334155), 0);
    lv_obj_add_event_cb(cls, expand_close_cb, LV_EVENT_CLICKED, (void *)(intptr_t)slot);
    lv_obj_t *clsl = lv_label_create(cls);
    lv_label_set_text(clsl, LV_SYMBOL_CLOSE);
    lv_obj_center(clsl);

    // present-state line (all current metrics)
    if (seed->detail && seed->detail[0]) {
        lv_obj_t *st = lv_label_create(panel);
        lv_label_set_text(st, seed->detail);
        lv_label_set_long_mode(st, LV_LABEL_LONG_WRAP);
        lv_obj_set_width(st, lv_pct(100));
        lv_obj_set_style_text_color(st, lv_color_hex(0xcbd5e1), 0);
        lv_obj_set_style_pad_top(st, 6, 0);
    }

    if (x->ngraph == 0) {
        lv_obj_t *none = lv_label_create(panel);
        lv_label_set_text(none, "no graphable metrics");
        lv_obj_set_style_text_color(none, lv_color_hex(0x64748b), 0);
        return;
    }

    // one chart per graphable metric (empty; the worker fills them after fetching)
    for (int i = 0; i < x->ngraph; i++) {
        lv_obj_t *lbl = lv_label_create(panel);
        lv_label_set_text(lbl, x->g[i].label);
        lv_obj_set_style_text_color(lbl, lv_color_hex(x->g[i].color), 0);
        lv_obj_set_style_pad_top(lbl, 10, 0);

        lv_obj_t *ch = lv_chart_create(panel);
        lv_obj_set_width(ch, lv_pct(100));
        lv_obj_set_height(ch, 150);
        lv_obj_set_style_bg_color(ch, lv_color_hex(0x0b1021), 0);
        lv_obj_set_style_border_width(ch, 0, 0);
        lv_obj_set_style_pad_all(ch, 4, 0);
        lv_chart_set_type(ch, LV_CHART_TYPE_LINE);
        lv_chart_set_update_mode(ch, LV_CHART_UPDATE_MODE_SHIFT);
        lv_chart_set_div_line_count(ch, 3, 0);
        lv_chart_set_point_count(ch, GRAPH_POINTS);
        lv_obj_set_style_width(ch, 0, LV_PART_INDICATOR);     // no per-point dots (dense series)
        lv_obj_set_style_height(ch, 0, LV_PART_INDICATOR);
        lv_chart_series_t *ser = lv_chart_add_series(ch, lv_color_hex(x->g[i].color),
                                                     LV_CHART_AXIS_PRIMARY_Y);
        x->chart[i] = ch;
        x->ser[i] = ser;

        lv_obj_t *nt = lv_label_create(panel);
        lv_label_set_text(nt, "loading…");
        lv_obj_set_style_text_color(nt, lv_color_hex(0x64748b), 0);
        x->note[i] = nt;
    }

    ui_chart_request(slot, x->epoch);   // hand the 72h fetch to ui/ui_chart's worker
}
