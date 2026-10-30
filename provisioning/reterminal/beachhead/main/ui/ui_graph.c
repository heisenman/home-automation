// Graph builder view (see ui_graph.h). A shared range control (6h/24h/7d/30d + Custom) over ONE graph
// panel: an lv_dropdown trace picker, up to MAX_SEL traces overlaid on one time-scaled lv_chart (SCATTER,
// x = seconds into the window), a time axis (ui_graph_axis) and a legend with each trace's color, real
// range and a remove button. Same unit -> one real scale; mixed units -> each trace normalized to its own
// 0-100 % (the PWA AdaptiveChart's rule). Fed by a dedicated fetch worker (local-SD rung replica first ->
// BFF /devices/<id>/readings fallback, generation-guarded like ui_chart), one trace after another.
// Multi-panel (P2d) and weather traces (P2e) build on this. docs/design/d1001-graph-builder.md.
#include "ui/ui_graph.h"
#include "ui/ui_http.h"       // ui_http_get / ui_http_base
#include "ui/ui_format.h"     // ascii_fold, disp_val, disp_unit
#include "ui/ui_chart.h"      // GRAPH_POINTS (shared, PSRAM-tuned)
#include "ui/ui_graph_axis.h" // time axis: day lines, hour ticks, labels
#include "ui/ui_daterange.h"  // Custom range picker
#include "ha_replica.h"       // local SD rung replica (ADR-0022)
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
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
#define NORM_SPAN   10000      // normalized traces: 0..100 % in fixed point
#define MAX_TRACES  160        // trace-catalog cap (log if exceeded)
#define MAX_SEL     6          // traces on the one graph (design log §4: MAX_TRACES_PER_PANEL)
#define CHART_H     220
#define YAX_W       80
#define CLOCK_SET   1577836800 // 2020-01-01: before this the wall clock isn't set (RTC invalid + no SNTP)

// the PWA's trace palette, in the same order (a trace's color = its position on the graph)
static const uint32_t PALETTE[] = {0x4aa3ff, 0x34d399, 0xfbbf24, 0xf87171, 0xa78bfa, 0x22d3ee, 0xfb923c, 0xf472b6};
#define N_PALETTE ((int)(sizeof(PALETTE) / sizeof(PALETTE[0])))

// Range presets (design log §3). A preset ends now; Custom is a fixed window from ui_daterange.
static const struct { const char *label; int hours; } RANGES[] = {
    {"6h", 6}, {"24h", 24}, {"7d", 168}, {"30d", 720},
};
#define N_RANGES ((int)(sizeof(RANGES) / sizeof(RANGES[0])))

static int s_hours = 24;                       // the preset; 0 = Custom (s_c0..s_c1)
static time_t s_c0, s_c1;
static lv_obj_t *s_range_btn[N_RANGES], *s_custom_btn, *s_range_note;

// Trace catalog: one entry per (sensor × its server-authored `graphs` metric). Strings are copied out
// of the cJSON so the catalog outlives the parse.
struct gtrace { char label[64]; char unit[12]; char source[40]; char metric[24]; };
static struct gtrace s_cat[MAX_TRACES];
static int s_ncat;

// the graph: chosen traces (catalog indices, in the order added) + its widgets
static int s_sel[MAX_SEL];
static int s_nsel;
static lv_obj_t *s_dd, *s_chart, *s_layer, *s_xrow, *s_yax[3], *s_note, *s_legend;
static volatile uint32_t s_gen;                // bumped on trace/range/nav change; stale fetches discard

// one fetched trace (display units, oldest -> newest); MAX_SEL of them in PSRAM (ui_graph_start)
struct gbuf { long t[GRAPH_POINTS]; double v[GRAPH_POINTS]; int n; };
static struct gbuf *s_buf;

struct graph_req { uint32_t gen; };
static QueueHandle_t s_gq;

// a trace to open with on the next render (ui_graph_preselect, from the Sensors grid's value tap)
static bool s_pre;
static char s_pre_id[40], s_pre_metric[24];
static int s_pre_hours;

int ui_graph_hours(void) { return s_hours > 0 ? s_hours : (int)((s_c1 - s_c0) / 3600); }

void ui_graph_preselect(const char *device_id, const char *metric, int hours)
{
    snprintf(s_pre_id, sizeof s_pre_id, "%s", device_id ? device_id : "");
    snprintf(s_pre_metric, sizeof s_pre_metric, "%s", metric ? metric : "");
    s_pre_hours = hours;
    s_pre = true;
}

static bool clock_ok(void) { return time(NULL) > CLOCK_SET; }

// the window being shown; false = the clock isn't set (then only "last N hours", with no time axis)
static bool window(time_t *t0, time_t *t1)
{
    if (!clock_ok()) return false;
    if (s_hours > 0) { *t1 = time(NULL); *t0 = *t1 - (time_t)s_hours * 3600; }
    else             { *t0 = s_c0; *t1 = s_c1; }
    return true;
}

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

// ── time helpers (the BFF speaks ISO-8601 UTC; newlib has no timegm) ─────────
static long days_from_civil(int y, int m, int d)
{
    y -= m <= 2;
    long era = (y >= 0 ? y : y - 399) / 400;
    int yoe = (int)(y - era * 400);
    int doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    int doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + doe - 719468;
}

static long parse_iso(const char *s)            // "2026-10-30T01:02:03[.fff][Z|+00:00]" -> epoch; -1 on a miss
{
    int Y, M, D, h, mi, sec;
    if (!s || sscanf(s, "%d-%d-%dT%d:%d:%d", &Y, &M, &D, &h, &mi, &sec) != 6) return -1;
    return days_from_civil(Y, M, D) * 86400L + h * 3600L + mi * 60L + sec;
}

static void fmt_iso(time_t t, char *out, size_t cap)
{
    struct tm g;
    gmtime_r(&t, &g);
    strftime(out, cap, "%Y-%m-%dT%H:%M:%SZ", &g);
}

// ── fetch worker (off the LVGL/click stack) ──────────────────────────────────
// One trace into `b`: local replica first (timestamped + binned when the clock is set), else the BFF.
// With no clock, x is just the point index (the old P2b behaviour).
static void fetch_trace(const char *id, const char *metric, const char *unit, bool timed,
                        time_t t0, time_t t1, int hours, struct gbuf *b)
{
    b->n = 0;
    int m;
    if (timed) {
        m = ha_replica_rung_range(id, metric, (long)t0, (long)t1, b->t, b->v, GRAPH_POINTS);
    } else {
        m = ha_replica_rung_query(id, metric, hours, b->v, GRAPH_POINTS);
        for (int i = 0; i < m; i++) b->t[i] = i;
    }
    if (m > 0) {
        b->n = m;
    } else {
        char url[384];
        if (timed) {
            char a[24], z[24];
            fmt_iso(t0, a, sizeof a);
            fmt_iso(t1, z, sizeof z);
            snprintf(url, sizeof url, "%s/devices/%s/readings?metric=%s&start=%s&end=%s&res=auto&limit=%d",
                     ui_http_base(), id, metric, a, z, GRAPH_POINTS);
        } else {
            snprintf(url, sizeof url, "%s/devices/%s/readings?metric=%s&hours=%d&res=auto&limit=%d",
                     ui_http_base(), id, metric, hours, GRAPH_POINTS);
        }
        int len = 0;
        char *buf = ui_http_get(url, &len);
        if (buf && len > 0) {
            cJSON *root = cJSON_Parse(buf);
            cJSON *arr = root ? cJSON_GetObjectItem(root, "readings") : NULL, *r;
            cJSON_ArrayForEach(r, arr) {                   // the server decimates evenly to `limit`
                if (b->n >= GRAPH_POINTS) break;
                cJSON *v = cJSON_GetObjectItem(r, "value"), *ts = cJSON_GetObjectItem(r, "ts");
                if (!cJSON_IsNumber(v)) continue;
                long t = timed ? parse_iso(cJSON_IsString(ts) ? ts->valuestring : NULL) : b->n;
                if (t < 0) continue;
                b->t[b->n] = t;
                b->v[b->n] = v->valuedouble;
                b->n++;
            }
            if (root) cJSON_Delete(root);
        }
        if (buf) heap_caps_free(buf);
    }
    for (int i = 0; i < b->n; i++) b->v[i] = disp_val(unit, b->v[i]);   // °C -> °F for display
    ESP_LOGI(TAG, "graph %s/%s <- %s (%d pts, %s)", id, metric, m > 0 ? "rung(SD)" : "http", b->n,
             timed ? "timed" : "no clock");
}

static void fmt_num(double v, char *out, size_t cap)
{
    if (v > -1000 && v < 1000) snprintf(out, cap, "%.1f", v);
    else snprintf(out, cap, "%.0f", v);
}

static void on_remove(lv_event_t *e);

// one legend row per chosen trace: swatch | label (in its color) | real range | remove
static void legend_build(char (*ranges)[40])
{
    lv_obj_clean(s_legend);
    for (int k = 0; k < s_nsel; k++) {
        uint32_t col = PALETTE[k % N_PALETTE];
        lv_obj_t *r = lv_obj_create(s_legend);
        lv_obj_remove_style_all(r);
        lv_obj_set_size(r, lv_pct(100), LV_SIZE_CONTENT);
        lv_obj_set_style_pad_column(r, 10, 0);
        lv_obj_set_flex_flow(r, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(r, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);

        lv_obj_t *sw = lv_obj_create(r);
        lv_obj_remove_style_all(sw);
        lv_obj_set_size(sw, 16, 16);
        lv_obj_set_style_radius(sw, 3, 0);
        lv_obj_set_style_bg_color(sw, lv_color_hex(col), 0);
        lv_obj_set_style_bg_opa(sw, LV_OPA_COVER, 0);

        lv_obj_t *l = lv_label_create(r);
        lv_label_set_text(l, s_cat[s_sel[k]].label);
        lv_label_set_long_mode(l, LV_LABEL_LONG_DOT);
        lv_obj_set_width(l, 1);
        lv_obj_set_flex_grow(l, 1);
        lv_obj_set_style_text_font(l, &lv_font_montserrat_20, 0);
        lv_obj_set_style_text_color(l, lv_color_hex(col), 0);

        lv_obj_t *rg = lv_label_create(r);
        lv_label_set_text(rg, ranges ? ranges[k] : "");
        lv_obj_set_style_text_font(rg, &lv_font_montserrat_14, 0);
        lv_obj_set_style_text_color(rg, lv_color_hex(0x94a3b8), 0);

        lv_obj_t *x = lv_obj_create(r);
        lv_obj_set_size(x, 52, 40);
        lv_obj_set_style_bg_color(x, lv_color_hex(0x1e293b), 0);
        lv_obj_set_style_border_color(x, lv_color_hex(0x2f7e7a), 0);
        lv_obj_set_style_border_width(x, 2, 0);
        lv_obj_set_style_radius(x, 8, 0);
        lv_obj_set_style_pad_all(x, 0, 0);
        lv_obj_clear_flag(x, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(x, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(x, on_remove, LV_EVENT_CLICKED, (void *)(intptr_t)k);
        lv_obj_t *xl = lv_label_create(x);
        lv_label_set_text(xl, LV_SYMBOL_CLOSE);
        lv_obj_set_style_text_font(xl, &lv_font_montserrat_20, 0);
        lv_obj_set_style_text_color(xl, lv_color_hex(0xffffff), 0);
        lv_obj_center(xl);
    }
}

static void chart_clear(void)
{
    lv_chart_series_t *ser;
    while ((ser = lv_chart_get_series_next(s_chart, NULL)) != NULL) lv_chart_remove_series(s_chart, ser);
    lv_obj_clean(s_layer);
    lv_obj_clean(s_xrow);
    for (int i = 0; i < 3; i++) lv_label_set_text(s_yax[i], "");
    lv_chart_refresh(s_chart);
}

// Fill the chart from s_buf[0..n) (LVGL lock held, generation already checked)
static void apply(int n, char (*unit)[12], bool timed, time_t t0, time_t t1)
{
    double mn[MAX_SEL], mx[MAX_SEL];
    bool have[MAX_SEL], shared = true;
    const char *u0 = NULL;
    int nhave = 0;
    for (int k = 0; k < n; k++) {
        struct gbuf *b = &s_buf[k];
        have[k] = b->n > 0;
        if (!have[k]) continue;
        nhave++;
        mn[k] = mx[k] = b->v[0];
        for (int i = 1; i < b->n; i++) {
            if (b->v[i] < mn[k]) mn[k] = b->v[i];
            if (b->v[i] > mx[k]) mx[k] = b->v[i];
        }
        if (!u0) u0 = unit[k];
        else if (strcmp(u0, unit[k]) != 0) shared = false;
    }

    chart_clear();
    char ranges[MAX_SEL][40];
    for (int k = 0; k < s_nsel; k++) snprintf(ranges[k], sizeof ranges[k], "%s", k < n && !have[k] ? "no data" : "");
    if (!nhave) {
        legend_build(ranges);
        lv_label_set_text(s_note, "no data in range");
        return;
    }

    double lo = 0, hi = 0;
    bool first = true;
    for (int k = 0; k < n; k++) {
        if (!have[k]) continue;
        if (first || mn[k] < lo) lo = mn[k];
        if (first || mx[k] > hi) hi = mx[k];
        first = false;
    }
    if (lo == hi) { lo -= 1; hi += 1; }                       // avoid a zero-height axis
    lv_chart_set_point_count(s_chart, GRAPH_POINTS);          // before the series: they start all-NONE
    lv_chart_set_axis_range(s_chart, LV_CHART_AXIS_PRIMARY_X, 0, timed ? (int32_t)(t1 - t0) : 1000);
    if (shared) lv_chart_set_axis_range(s_chart, LV_CHART_AXIS_PRIMARY_Y, (int32_t)(lo * GP_SCALE), (int32_t)(hi * GP_SCALE));
    else        lv_chart_set_axis_range(s_chart, LV_CHART_AXIS_PRIMARY_Y, 0, NORM_SPAN);

    for (int k = 0; k < n; k++) {
        if (!have[k]) continue;
        struct gbuf *b = &s_buf[k];
        lv_chart_series_t *ser = lv_chart_add_series(s_chart, lv_color_hex(PALETTE[k % N_PALETTE]), LV_CHART_AXIS_PRIMARY_Y);
        if (!ser) break;
        double kmn = mn[k], kmx = mx[k];
        if (kmn == kmx) { kmn -= 1; kmx += 1; }
        for (int i = 0; i < b->n; i++) {
            int32_t x = timed ? (int32_t)(b->t[i] - t0) : (b->n > 1 ? i * 1000 / (b->n - 1) : 500);
            int32_t y = shared ? (int32_t)(b->v[i] * GP_SCALE) : (int32_t)((b->v[i] - kmn) / (kmx - kmn) * NORM_SPAN);
            lv_chart_set_series_value_by_id2(s_chart, ser, i, x, y);
        }
        char a[16], z[16];
        fmt_num(mn[k], a, sizeof a);
        fmt_num(mx[k], z, sizeof z);
        snprintf(ranges[k], sizeof ranges[k], "%s - %s %s", a, z, disp_unit(unit[k]));
    }
    lv_chart_refresh(s_chart);

    if (shared) {
        char t[24], v[16];
        const char *du = disp_unit(u0);
        fmt_num(hi, v, sizeof v);              snprintf(t, sizeof t, "%s %s", v, du); lv_label_set_text(s_yax[0], t);
        fmt_num((lo + hi) / 2, v, sizeof v);   snprintf(t, sizeof t, "%s %s", v, du); lv_label_set_text(s_yax[1], t);
        fmt_num(lo, v, sizeof v);              snprintf(t, sizeof t, "%s %s", v, du); lv_label_set_text(s_yax[2], t);
    } else {
        lv_label_set_text(s_yax[0], "100%");
        lv_label_set_text(s_yax[1], "50%");
        lv_label_set_text(s_yax[2], "0%");
    }
    if (timed) ui_graph_axis_draw(s_layer, s_xrow, t0, t1);
    legend_build(ranges);
    lv_label_set_text(s_note, !timed ? "clock not set - no time axis"
                            : shared ? "" : "normalized - mixed units (real ranges below)");
}

static void graph_worker(void *pv)
{
    (void)pv;
    struct graph_req rq;
    static char id[MAX_SEL][40], metric[MAX_SEL][24], unit[MAX_SEL][12];
    for (;;) {
        if (xQueueReceive(s_gq, &rq, portMAX_DELAY) != pdTRUE) continue;
        // snapshot the chosen traces + window under the lock (so a concurrent re-pick can't tear it)
        int n = 0, hours = 24;
        time_t t0 = 0, t1 = 0;
        bool timed = false;
        if (lvgl_port_lock(0)) {
            if (rq.gen == s_gen) {
                n = s_nsel;
                for (int k = 0; k < n; k++) {
                    const struct gtrace *t = &s_cat[s_sel[k]];
                    snprintf(id[k], sizeof id[k], "%s", t->source);
                    snprintf(metric[k], sizeof metric[k], "%s", t->metric);
                    snprintf(unit[k], sizeof unit[k], "%s", t->unit);
                }
                hours = s_hours > 0 ? s_hours : 24;
                timed = window(&t0, &t1);
            }
            lvgl_port_unlock();
        }
        if (n == 0 || !s_buf) continue;

        for (int k = 0; k < n && rq.gen == s_gen; k++)        // sequential (design log §3); stop if superseded
            fetch_trace(id[k], metric[k], unit[k], timed, t0, t1, hours, &s_buf[k]);

        // fill the chart under the lock, discarding if a re-pick/nav bumped the generation mid-fetch
        if (lvgl_port_lock(0)) {
            if (rq.gen == s_gen && s_chart) apply(n, unit, timed, t0, t1);
            lvgl_port_unlock();
        }
    }
}

// (re)fetch everything on the graph — after any trace or range change (LVGL lock held)
static void refresh(void)
{
    s_gen++;
    legend_build(NULL);
    if (!s_nsel) {
        chart_clear();
        lv_label_set_text(s_note, s_ncat ? "add a trace to plot" : "no graphable metrics available");
        return;
    }
    lv_label_set_text(s_note, "loading...");
    if (!s_gq) return;
    struct graph_req rq = { .gen = s_gen };
    xQueueSend(s_gq, &rq, 0);
}

// refresh() rebuilds the legend, which holds the button being clicked — so it runs after the event
static void refresh_async(void *ud) { (void)ud; if (s_chart) refresh(); }

static void on_remove(lv_event_t *e)
{
    int k = (int)(intptr_t)lv_event_get_user_data(e);
    if (k < 0 || k >= s_nsel) return;
    ESP_LOGI(TAG, "remove trace %s", s_cat[s_sel[k]].label);
    for (int i = k; i < s_nsel - 1; i++) s_sel[i] = s_sel[i + 1];
    s_nsel--;
    lv_async_call(refresh_async, NULL);
}

// ── range control ────────────────────────────────────────────────────────────
static void style_range_btn(lv_obj_t *btn, bool active)
{
    lv_obj_set_style_bg_color(btn, lv_color_hex(active ? 0x2f7e7a : 0x16204a), 0);
    lv_obj_set_style_border_color(btn, lv_color_hex(active ? 0x5eead4 : 0x2f7e7a), 0);
}

static void style_ranges(void)
{
    for (int i = 0; i < N_RANGES; i++)
        if (s_range_btn[i]) style_range_btn(s_range_btn[i], RANGES[i].hours == s_hours);
    if (s_custom_btn) style_range_btn(s_custom_btn, s_hours == 0);
    if (!s_range_note) return;
    if (s_hours == 0) {
        struct tm a, z;
        localtime_r(&s_c0, &a);
        localtime_r(&s_c1, &z);
        char t[64];
        snprintf(t, sizeof t, "%d/%d %02d:%02d  -  %d/%d %02d:%02d", a.tm_mon + 1, a.tm_mday, a.tm_hour, a.tm_min,
                 z.tm_mon + 1, z.tm_mday, z.tm_hour, z.tm_min);
        lv_label_set_text(s_range_note, t);
        lv_obj_clear_flag(s_range_note, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(s_range_note, LV_OBJ_FLAG_HIDDEN);
    }
}

static void range_clicked_cb(lv_event_t *e)
{
    int idx = (int)(intptr_t)lv_event_get_user_data(e);
    if (idx < 0 || idx >= N_RANGES) return;
    s_hours = RANGES[idx].hours;
    style_ranges();
    ESP_LOGI(TAG, "range -> %s (%dh)", RANGES[idx].label, s_hours);
    if (s_nsel) refresh();
}

static void on_custom_applied(time_t t0, time_t t1)
{
    s_hours = 0;
    s_c0 = t0;
    s_c1 = t1;
    style_ranges();
    if (s_nsel) refresh();
}

static void custom_clicked_cb(lv_event_t *e)
{
    (void)e;
    time_t t0, t1;
    if (!window(&t0, &t1)) { lv_label_set_text(s_note, "clock not set - custom range unavailable"); return; }
    ui_daterange_open(t0, t1, on_custom_applied);
}

// ── trace picker ─────────────────────────────────────────────────────────────
static void trace_dd_cb(lv_event_t *e)
{
    lv_obj_t *dd = lv_event_get_target(e);
    int sel = lv_dropdown_get_selected(dd) - 1;   // option 0 = "+ add trace..." placeholder
    lv_dropdown_set_selected(dd, 0);
    if (sel < 0 || sel >= s_ncat) return;
    for (int k = 0; k < s_nsel; k++)
        if (s_sel[k] == sel) { lv_label_set_text(s_note, "already on the graph"); return; }
    if (s_nsel >= MAX_SEL) {
        ESP_LOGW(TAG, "trace limit (%d) reached", MAX_SEL);   // no-silent-caps: log + say so
        lv_label_set_text(s_note, "at most 6 traces - remove one first");
        return;
    }
    s_sel[s_nsel++] = sel;
    ESP_LOGI(TAG, "add trace %s (%d on the graph)", s_cat[sel].label, s_nsel);
    refresh();
}

// ── render ───────────────────────────────────────────────────────────────────
static lv_obj_t *range_btn(lv_obj_t *row, const char *txt, int w, lv_event_cb_t cb, void *ud)
{
    lv_obj_t *btn = lv_obj_create(row);
    lv_obj_set_size(btn, w, 46);
    lv_obj_set_style_border_width(btn, 2, 0);
    lv_obj_set_style_radius(btn, 10, 0);
    lv_obj_set_style_pad_all(btn, 0, 0);
    lv_obj_clear_flag(btn, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(btn, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(btn, cb, LV_EVENT_CLICKED, ud);
    lv_obj_t *l = lv_label_create(btn);
    lv_label_set_text(l, txt);
    lv_obj_set_style_text_font(l, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(0xffffff), 0);
    lv_obj_center(l);
    return btn;
}

static lv_obj_t *bare(lv_obj_t *parent)        // a transparent, unpadded, non-scrolling box
{
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_remove_style_all(o);
    lv_obj_clear_flag(o, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    return o;
}

void ui_graph_render(cJSON *sensors, lv_obj_t *parent)
{
    s_gen++;                                     // invalidate any in-flight fetch against old objects
    s_nsel = 0;
    s_chart = NULL;
    build_catalog(sensors);
    bool pre = s_pre;
    s_pre = false;
    if (pre) {
        for (int i = 0; i < N_RANGES; i++)
            if (RANGES[i].hours == s_pre_hours) s_hours = s_pre_hours;   // only a preset (the buttons show it)
    }
    if (s_hours == 0 && !clock_ok()) s_hours = 24;                       // a Custom window needs the clock

    lv_obj_clean(parent);                        // rebuild from scratch on nav-in
    lv_obj_set_flex_flow(parent, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(parent, 10, 0);

    // ── range control row (presets + Custom) ──
    lv_obj_t *row = lv_obj_create(parent);
    lv_obj_set_width(row, lv_pct(100));
    lv_obj_set_height(row, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(row, 0, 0);
    lv_obj_set_style_border_width(row, 0, 0);
    lv_obj_set_style_pad_all(row, 0, 0);
    lv_obj_set_style_pad_column(row, 8, 0);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
    for (int i = 0; i < N_RANGES; i++) s_range_btn[i] = range_btn(row, RANGES[i].label, 88, range_clicked_cb, (void *)(intptr_t)i);
    s_custom_btn = range_btn(row, LV_SYMBOL_EDIT "  Custom", 150, custom_clicked_cb, NULL);
    s_range_note = lv_label_create(parent);
    lv_obj_set_style_text_font(s_range_note, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(s_range_note, lv_color_hex(0x5eead4), 0);
    style_ranges();

    // ── the graph panel: trace picker, plot (y labels | chart over x labels), note, legend ──
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
    s_dd = lv_dropdown_create(panel);
    size_t cap = 32 + (size_t)s_ncat * (sizeof(((struct gtrace *)0)->label) + 1);
    char *opts = malloc(cap);
    if (opts) {
        size_t o = 0;
        o += snprintf(opts + o, cap - o, "+ add trace...");
        for (int i = 0; i < s_ncat && o < cap - 2; i++)
            o += snprintf(opts + o, cap - o, "\n%s", s_cat[i].label);
        lv_dropdown_set_options(s_dd, opts);     // copies the string
        free(opts);
    }
    lv_obj_set_width(s_dd, lv_pct(100));
    lv_obj_add_event_cb(s_dd, trace_dd_cb, LV_EVENT_VALUE_CHANGED, NULL);

    lv_obj_t *plot = bare(panel);
    lv_obj_set_size(plot, lv_pct(100), LV_SIZE_CONTENT);
    lv_obj_set_style_pad_column(plot, 8, 0);
    lv_obj_set_flex_flow(plot, LV_FLEX_FLOW_ROW);

    lv_obj_t *yax = bare(plot);                  // y labels: top / middle / bottom of the plot
    lv_obj_set_size(yax, YAX_W, CHART_H);
    lv_obj_set_flex_flow(yax, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(yax, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_END, LV_FLEX_ALIGN_END);
    for (int i = 0; i < 3; i++) {
        s_yax[i] = lv_label_create(yax);
        lv_label_set_text(s_yax[i], "");
        lv_obj_set_width(s_yax[i], lv_pct(100));
        lv_obj_set_style_text_align(s_yax[i], LV_TEXT_ALIGN_RIGHT, 0);
        lv_obj_set_style_text_font(s_yax[i], &lv_font_montserrat_14, 0);
        lv_obj_set_style_text_color(s_yax[i], lv_color_hex(0x94a3b8), 0);
    }

    lv_obj_t *pcol = bare(plot);
    lv_obj_set_height(pcol, LV_SIZE_CONTENT);
    lv_obj_set_width(pcol, 1);
    lv_obj_set_flex_grow(pcol, 1);
    lv_obj_set_style_pad_row(pcol, 4, 0);
    lv_obj_set_flex_flow(pcol, LV_FLEX_FLOW_COLUMN);

    s_chart = lv_chart_create(pcol);
    lv_obj_set_size(s_chart, lv_pct(100), CHART_H);
    lv_chart_set_type(s_chart, LV_CHART_TYPE_SCATTER);
    lv_chart_set_point_count(s_chart, GRAPH_POINTS);
    lv_chart_set_div_line_count(s_chart, 3, 0);                 // top / middle / bottom; day lines are ours
    lv_obj_set_style_pad_all(s_chart, 0, 0);                    // plot area = the whole chart (axis maths)
    lv_obj_set_style_border_width(s_chart, 0, 0);
    lv_obj_set_style_radius(s_chart, 0, 0);
    lv_obj_set_style_bg_color(s_chart, lv_color_hex(0x0b1021), 0);
    lv_obj_set_style_line_color(s_chart, lv_color_hex(0x334155), LV_PART_MAIN);
    lv_obj_set_style_line_width(s_chart, 2, LV_PART_ITEMS);
    lv_obj_set_style_width(s_chart, 0, LV_PART_INDICATOR);     // no point markers, just lines
    lv_obj_set_style_height(s_chart, 0, LV_PART_INDICATOR);
    lv_obj_clear_flag(s_chart, LV_OBJ_FLAG_SCROLLABLE);
    s_layer = bare(s_chart);                                    // day lines + hour ticks over the plot
    lv_obj_set_size(s_layer, lv_pct(100), lv_pct(100));

    s_xrow = bare(pcol);                                        // x labels under the plot
    lv_obj_set_size(s_xrow, lv_pct(100), 20);

    s_note = lv_label_create(panel);
    lv_label_set_text(s_note, "");
    lv_obj_set_style_text_font(s_note, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(s_note, lv_color_hex(0x94a3b8), 0);

    s_legend = bare(panel);
    lv_obj_set_size(s_legend, lv_pct(100), LV_SIZE_CONTENT);
    lv_obj_set_style_pad_row(s_legend, 6, 0);
    lv_obj_set_flex_flow(s_legend, LV_FLEX_FLOW_COLUMN);

    if (pre) {                                   // opened from the Sensors grid: that trace, fetching
        for (int i = 0; i < s_ncat; i++)
            if (strcmp(s_cat[i].source, s_pre_id) == 0 && strcmp(s_cat[i].metric, s_pre_metric) == 0) {
                s_sel[s_nsel++] = i;
                break;
            }
        ESP_LOGI(TAG, "preselect %s/%s -> %s", s_pre_id, s_pre_metric, s_nsel ? "found" : "not in catalog");
    }
    refresh();
    if (pre && !s_nsel) lv_label_set_text(s_note, "no graph for that reading");

    ESP_LOGI(TAG, "graphs view rendered (range=%dh, %d traces in catalog)", ui_graph_hours(), s_ncat);
}

void ui_graph_start(void)
{
    s_buf = heap_caps_calloc(MAX_SEL, sizeof(struct gbuf), MALLOC_CAP_SPIRAM);
    if (!s_buf) ESP_LOGE(TAG, "no PSRAM for graph buffers — graphs disabled");
    s_gq = xQueueCreate(4, sizeof(struct graph_req));
    // 16 KB stack: the worker opens sqlite (ha_replica_rung_range), same as ui_chart's worker.
    xTaskCreate(graph_worker, "uigraph", 16384, NULL, 4, NULL);
}
