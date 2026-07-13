// Graph builder view (see ui_graph.h). Phase 2b: a shared range control + ONE panel with an
// lv_dropdown trace picker and one lv_chart, fed by a dedicated fetch worker (local-SD rung replica
// first → BFF /devices/<id>/readings fallback, generation-guarded like ui_chart). Multi-trace
// overlay (P2c) and multi-panel (P2d) build on this. docs/design/d1001-graph-builder.md.
#include "ui/ui_graph.h"
#include "ui/ui_http.h"     // ui_http_get / ui_http_base
#include "ui/ui_format.h"   // ascii_fold, disp_val
#include "ui/ui_chart.h"    // GRAPH_POINTS (shared, PSRAM-tuned)
#include "ha_replica.h"     // ha_replica_rung_query — local SD rung replica (ADR-0022)
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "cJSON.h"
#include "lvgl.h"
#include "esp_lvgl_port.h"

static const char *TAG = "ui.graph";

#define GP_SCALE    100        // int fixed-point for lv_chart (2 decimals), matches ui_chart
#define MAX_TRACES  160        // trace-catalog cap (log if exceeded)

// Range presets (design log §3). `hours` drives the fetch (ha_replica rung horizon / readings query).
static const struct { const char *label; int hours; } RANGES[] = {
    {"6h", 6}, {"24h", 24}, {"7d", 168}, {"30d", 720},
};
#define N_RANGES ((int)(sizeof(RANGES) / sizeof(RANGES[0])))

static int s_hours = 24;                       // default 24h (matches the PWA)
static lv_obj_t *s_range_btn[N_RANGES];

// Trace catalog: one entry per (sensor × its server-authored `graphs` metric). Strings are copied out
// of the cJSON so the catalog outlives the parse.
struct gtrace { char label[64]; char unit[12]; char source[40]; char metric[24]; };
static struct gtrace s_cat[MAX_TRACES];
static int s_ncat;

// P2b: ONE panel = one chart + one selected trace (multi-trace overlay lands in P2c).
static lv_obj_t *s_chart;
static lv_chart_series_t *s_ser;
static lv_obj_t *s_note;
static int s_trace = -1;                        // catalog index, -1 = none picked
static uint32_t s_gen;                          // bumped on trace/range/nav change; stale fetches discard

struct graph_req { uint32_t gen; };
static QueueHandle_t s_gq;

int ui_graph_hours(void) { return s_hours; }

// ── trace catalog ────────────────────────────────────────────────────────────
static void build_catalog(cJSON *sensors)
{
    s_ncat = 0;
    if (!cJSON_IsArray(sensors)) return;
    cJSON *s;
    cJSON_ArrayForEach(s, sensors) {
        if (s_ncat >= MAX_TRACES) { ESP_LOGW(TAG, "trace catalog hit MAX_TRACES=%d — truncated", MAX_TRACES); break; }
        const cJSON *jid = cJSON_GetObjectItem(s, "device_id");
        const cJSON *jnm = cJSON_GetObjectItem(s, "name");
        if (!cJSON_IsString(jid)) continue;
        const char *id = jid->valuestring;
        const char *nm = (cJSON_IsString(jnm) && jnm->valuestring[0]) ? jnm->valuestring : id;
        cJSON *graphs = cJSON_GetObjectItem(s, "graphs");   // server-authored graphable set (ADR-0019)
        if (!cJSON_IsArray(graphs)) continue;
        cJSON *g;
        cJSON_ArrayForEach(g, graphs) {
            if (s_ncat >= MAX_TRACES) break;
            const cJSON *gk = cJSON_GetObjectItem(g, "key");
            const cJSON *gl = cJSON_GetObjectItem(g, "label");
            const cJSON *gu = cJSON_GetObjectItem(g, "unit");
            if (!cJSON_IsString(gk)) continue;
            struct gtrace *t = &s_cat[s_ncat++];
            char nmf[28], glf[20];
            ascii_fold(nm, nmf, sizeof nmf);
            ascii_fold(cJSON_IsString(gl) ? gl->valuestring : gk->valuestring, glf, sizeof glf);
            snprintf(t->label, sizeof t->label, "%s - %s", nmf, glf);
            ascii_fold(cJSON_IsString(gu) ? gu->valuestring : "", t->unit, sizeof t->unit);
            snprintf(t->source, sizeof t->source, "%s", id);
            snprintf(t->metric, sizeof t->metric, "%s", gk->valuestring);
        }
    }
    ESP_LOGI(TAG, "trace catalog: %d traces", s_ncat);
}

// ── fetch worker (off the LVGL/click stack) ──────────────────────────────────
static void graph_worker(void *pv)
{
    (void)pv;
    struct graph_req rq;
    static int32_t vals[GRAPH_POINTS];
    static double  raw[GRAPH_POINTS];
    for (;;) {
        if (xQueueReceive(s_gq, &rq, portMAX_DELAY) != pdTRUE) continue;
        // snapshot the selected trace + range under the lock (so a concurrent re-pick can't tear it)
        char id[40] = "", metric[24] = "", unit[12] = ""; int hours = 24; bool have = false;
        if (lvgl_port_lock(0)) {
            if (rq.gen == s_gen && s_trace >= 0 && s_trace < s_ncat) {
                snprintf(id, sizeof id, "%s", s_cat[s_trace].source);
                snprintf(metric, sizeof metric, "%s", s_cat[s_trace].metric);
                snprintf(unit, sizeof unit, "%s", s_cat[s_trace].unit);
                hours = s_hours;
                have = true;
            }
            lvgl_port_unlock();
        }
        if (!have) continue;

        int n = 0; int32_t vmin = 0, vmax = 0;
        // local-first: the resolution-selected vmean series from the SD rung replica; else the BFF.
        int m = ha_replica_rung_query(id, metric, hours, raw, GRAPH_POINTS);
        if (m > 0) {
            for (int k = 0; k < m; k++) {
                int32_t sv = (int32_t)(disp_val(unit, raw[k]) * GP_SCALE);   // °C→°F for temp
                vals[k] = sv;
                if (k == 0 || sv < vmin) vmin = sv;
                if (k == 0 || sv > vmax) vmax = sv;
            }
            n = m;
        } else {
            char url[384];
            snprintf(url, sizeof url, "%s/devices/%s/readings?metric=%s&hours=%d&limit=%d",
                     ui_http_base(), id, metric, hours, GRAPH_POINTS);
            int len = 0; char *buf = ui_http_get(url, &len);
            if (buf && len > 0) {
                cJSON *root = cJSON_Parse(buf);
                cJSON *arr = root ? cJSON_GetObjectItem(root, "readings") : NULL;
                if (cJSON_IsArray(arr)) {
                    cJSON *r;
                    cJSON_ArrayForEach(r, arr) {
                        if (n >= GRAPH_POINTS) break;
                        cJSON *v = cJSON_GetObjectItem(r, "value");
                        if (!cJSON_IsNumber(v)) continue;
                        int32_t sv = (int32_t)(disp_val(unit, v->valuedouble) * GP_SCALE);
                        vals[n] = sv;
                        if (n == 0 || sv < vmin) vmin = sv;
                        if (n == 0 || sv > vmax) vmax = sv;
                        n++;
                    }
                }
                if (root) cJSON_Delete(root);
            }
            if (buf) heap_caps_free(buf);
        }
        ESP_LOGI(TAG, "graph %s/%s <- %s (%d pts, %dh)", id, metric, m > 0 ? "rung(SD)" : "http", n, hours);

        // fill the chart under the lock, discarding if a re-pick/nav bumped the generation mid-fetch
        if (lvgl_port_lock(0)) {
            if (rq.gen == s_gen && s_chart && s_ser) {
                if (n < 1) {
                    if (s_note) lv_label_set_text(s_note, "no data in range");
                } else {
                    int32_t lo = vmin, hi = vmax;
                    if (lo == hi) { lo -= GP_SCALE; hi += GP_SCALE; }   // avoid a zero-height axis
                    lv_chart_set_range(s_chart, LV_CHART_AXIS_PRIMARY_Y, lo, hi);
                    lv_chart_set_point_count(s_chart, n);
                    for (int i = 0; i < n; i++) lv_chart_set_value_by_id(s_chart, s_ser, i, vals[i]);
                    lv_chart_refresh(s_chart);
                    if (s_note) {
                        char nb[72];
                        snprintf(nb, sizeof nb, "%.2f - %.2f  ·  %d pts  ·  %dh",
                                 (double)vmin / GP_SCALE, (double)vmax / GP_SCALE, n, hours);
                        lv_label_set_text(s_note, nb);
                    }
                }
            }
            lvgl_port_unlock();
        }
    }
}

static void request_fetch(void)
{
    if (!s_gq) return;
    struct graph_req rq = { .gen = s_gen };
    xQueueSend(s_gq, &rq, 0);
}

// ── range control ────────────────────────────────────────────────────────────
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
    if (s_trace >= 0) {                          // re-fetch the current trace at the new range
        s_gen++;
        if (s_note) lv_label_set_text(s_note, "loading...");
        request_fetch();
    }
}

// ── trace picker ─────────────────────────────────────────────────────────────
static void trace_dd_cb(lv_event_t *e)
{
    lv_obj_t *dd = lv_event_get_target(e);
    int sel = lv_dropdown_get_selected(dd);      // option 0 = "+ add trace..." placeholder
    if (sel <= 0 || sel - 1 >= s_ncat) { s_trace = -1; return; }
    s_trace = sel - 1;
    s_gen++;
    if (s_note) lv_label_set_text(s_note, "loading...");
    request_fetch();
}

void ui_graph_render(cJSON *sensors, lv_obj_t *parent)
{
    s_gen++;                                     // invalidate any in-flight fetch against old objects
    s_trace = -1; s_chart = NULL; s_ser = NULL; s_note = NULL;
    build_catalog(sensors);

    lv_obj_clean(parent);                        // rebuild from scratch on nav-in
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

    // ── one graph panel: trace picker + chart + status note ──
    lv_obj_t *panel = lv_obj_create(parent);
    lv_obj_set_width(panel, lv_pct(100));
    lv_obj_set_height(panel, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_color(panel, lv_color_hex(0x111a33), 0);
    lv_obj_set_style_border_width(panel, 0, 0);
    lv_obj_set_style_radius(panel, 12, 0);
    lv_obj_set_style_pad_all(panel, 10, 0);
    lv_obj_set_style_pad_row(panel, 8, 0);
    lv_obj_set_flex_flow(panel, LV_FLEX_FLOW_COLUMN);
    lv_obj_clear_flag(panel, LV_OBJ_FLAG_SCROLLABLE);

    // trace picker (lv_dropdown; option 0 = placeholder, 1..N = catalog)
    lv_obj_t *dd = lv_dropdown_create(panel);
    size_t cap = 32 + (size_t)s_ncat * (sizeof(((struct gtrace *)0)->label) + 1);
    char *opts = malloc(cap);
    if (opts) {
        size_t o = 0;
        o += snprintf(opts + o, cap - o, "+ add trace...");
        for (int i = 0; i < s_ncat && o < cap - 2; i++)
            o += snprintf(opts + o, cap - o, "\n%s", s_cat[i].label);
        lv_dropdown_set_options(dd, opts);       // copies the string
        free(opts);
    }
    lv_obj_set_width(dd, lv_pct(100));
    lv_obj_add_event_cb(dd, trace_dd_cb, LV_EVENT_VALUE_CHANGED, NULL);

    // chart
    s_chart = lv_chart_create(panel);
    lv_obj_set_width(s_chart, lv_pct(100));
    lv_obj_set_height(s_chart, 200);
    lv_chart_set_type(s_chart, LV_CHART_TYPE_LINE);
    lv_chart_set_point_count(s_chart, GRAPH_POINTS);
    lv_chart_set_div_line_count(s_chart, 4, 6);
    s_ser = lv_chart_add_series(s_chart, lv_color_hex(0x5eead4), LV_CHART_AXIS_PRIMARY_Y);

    // status / range note
    s_note = lv_label_create(panel);
    lv_label_set_text(s_note, s_ncat ? "pick a trace to plot" : "no graphable metrics available");
    lv_obj_set_style_text_font(s_note, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(s_note, lv_color_hex(0x94a3b8), 0);

    ESP_LOGI(TAG, "graphs view rendered (range=%dh, %d traces)", s_hours, s_ncat);
}

void ui_graph_start(void)
{
    s_gq = xQueueCreate(4, sizeof(struct graph_req));
    // 16 KB stack: the worker opens sqlite (ha_replica_rung_query), same as ui_chart's worker.
    xTaskCreate(graph_worker, "uigraph", 16384, NULL, 4, NULL);
}
