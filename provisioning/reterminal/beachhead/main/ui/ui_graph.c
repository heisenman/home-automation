// Graph builder view (see ui_graph.h). Phase 2a: the "Graphs" screen scaffold — a shared range
// control (presets) over a placeholder panel area. The trace picker, multi-series overlay, and the
// fetch worker land in P2b+ (docs/design/d1001-graph-builder.md §8).
#include "ui/ui_graph.h"
#include <stdio.h>
#include <stdint.h>
#include "esp_log.h"
#include "esp_lvgl_port.h"

static const char *TAG = "ui.graph";

// Range presets (design log §3). `hours` drives the readings fetch (ha_replica rung horizon / the
// BFF /devices/<id>/readings `hours` query). Mirrors the PWA RangeControl RANGES.
static const struct { const char *label; int hours; } RANGES[] = {
    {"6h", 6}, {"24h", 24}, {"7d", 168}, {"30d", 720},
};
#define N_RANGES ((int)(sizeof(RANGES) / sizeof(RANGES[0])))

static int s_hours = 24;                     // default 24h (matches the PWA default)
static lv_obj_t *s_range_btn[N_RANGES];      // preset buttons, restyled on selection

int ui_graph_hours(void) { return s_hours; }

static void style_range_btn(lv_obj_t *btn, bool active)
{
    lv_obj_set_style_bg_color(btn, lv_color_hex(active ? 0x2f7e7a : 0x16204a), 0);
    lv_obj_set_style_border_color(btn, lv_color_hex(active ? 0x5eead4 : 0x2f7e7a), 0);
}

static void range_clicked_cb(lv_event_t *e)
{
    int idx = (int)(intptr_t)lv_event_get_user_data(e);
    if (idx < 0 || idx >= N_RANGES) return;
    s_hours = RANGES[idx].hours;
    for (int i = 0; i < N_RANGES; i++)
        if (s_range_btn[i]) style_range_btn(s_range_btn[i], i == idx);
    ESP_LOGI(TAG, "range -> %s (%dh)", RANGES[idx].label, s_hours);
    // P2b+: re-fetch every open panel for the new range.
}

void ui_graph_render(lv_obj_t *parent)
{
    lv_obj_clean(parent);                    // rebuild from scratch on nav-in
    lv_obj_set_flex_flow(parent, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(parent, 10, 0);

    // ── range control row (presets) ──
    lv_obj_t *row = lv_obj_create(parent);
    lv_obj_set_width(row, lv_pct(100));
    lv_obj_set_height(row, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(row, 0, 0);
    lv_obj_set_style_border_width(row, 0, 0);
    lv_obj_set_style_pad_all(row, 0, 0);
    lv_obj_set_style_pad_column(row, 8, 0);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);

    for (int i = 0; i < N_RANGES; i++) {
        lv_obj_t *btn = lv_obj_create(row);
        lv_obj_set_size(btn, 88, 46);
        lv_obj_set_style_border_width(btn, 2, 0);
        lv_obj_set_style_radius(btn, 10, 0);
        lv_obj_set_style_pad_all(btn, 0, 0);
        lv_obj_clear_flag(btn, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(btn, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(btn, range_clicked_cb, LV_EVENT_CLICKED, (void *)(intptr_t)i);
        lv_obj_t *l = lv_label_create(btn);
        lv_label_set_text(l, RANGES[i].label);
        lv_obj_set_style_text_font(l, &lv_font_montserrat_20, 0);
        lv_obj_set_style_text_color(l, lv_color_hex(0xffffff), 0);
        lv_obj_center(l);
        s_range_btn[i] = btn;
        style_range_btn(btn, RANGES[i].hours == s_hours);
    }

    // ── placeholder (panels + trace picker land in P2b) ──
    lv_obj_t *ph = lv_label_create(parent);
    lv_label_set_text(ph, "Graph builder\nrange control ready - panels land in P2b");
    lv_obj_set_style_text_font(ph, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(ph, lv_color_hex(0x64748b), 0);
    lv_obj_set_style_pad_top(ph, 12, 0);

    ESP_LOGI(TAG, "graphs view rendered (range=%dh)", s_hours);
}
