// 72h chart fetch worker (see ui_chart.h). Extracted from ui_tiles.c verbatim (ADR-0020
// module-first); behavior identical. Runs OFF the click/LVGL stack: the fetch is unlocked,
// every s_exp[] touch is under the LVGL lock with an epoch re-check so a close mid-fetch
// discards the result safely.
#include "ui/ui_chart.h"
#include "ui/ui_expand.h"   // s_exp[] registry + struct expand_ref
#include "ui/ui_http.h"     // ui_http_get / ui_http_base (network fallback)
#include "ui/ui_format.h"   // struct gspec, MAX_GRAPHS, disp_val
#include "ha_replica.h"     // ha_replica_rung_query — local SD rung replica (ADR-0022 Phase 1b)
#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "cJSON.h"
#include "lvgl.h"
#include "esp_lvgl_port.h"

static const char *TAG = "ui.chart";
#define CHART_SCALE 100      // ints for lv_chart; preserves 2 decimals

struct chart_req { int slot; uint32_t epoch; };   // -> chart_worker (which expansion to fill)
static QueueHandle_t s_chart_q;    // int slot idx -> chart_worker (readings HTTP off the click stack)

// Populate chart #gi of slot from a fetched readings array. Holds the LVGL lock. Returns
// false if the expansion was closed/reused (epoch mismatch) — caller stops working it.
static bool chart_fill(int slot, uint32_t epoch, int gi, const int32_t *vals, int n,
                       int32_t vmin, int32_t vmax)
{
    if (!lvgl_port_lock(0)) return false;
    struct expand_ref *x = &s_exp[slot];
    bool ok = x->active && x->epoch == epoch && gi < x->ngraph && x->chart[gi] && x->ser[gi];
    if (ok) {
        lv_obj_t *ch = x->chart[gi];
        if (n < 1) {
            if (x->note[gi]) lv_label_set_text(x->note[gi], "no data (72h)");
        } else {
            int32_t lo = vmin, hi = vmax;
            if (lo == hi) { lo -= CHART_SCALE; hi += CHART_SCALE; }   // avoid a zero-height axis
            lv_chart_set_range(ch, LV_CHART_AXIS_PRIMARY_Y, lo, hi);
            lv_chart_set_point_count(ch, n);
            for (int i = 0; i < n; i++) lv_chart_set_value_by_id(ch, x->ser[gi], i, vals[i]);
            lv_chart_refresh(ch);
            if (x->note[gi]) {
                char nb[40];
                snprintf(nb, sizeof(nb), "%.2f – %.2f  ·  %d pts",
                         (double)vmin / CHART_SCALE, (double)vmax / CHART_SCALE, n);
                lv_label_set_text(x->note[gi], nb);
            }
        }
    }
    lvgl_port_unlock();
    return ok;
}

static void chart_worker(void *pv)
{
    struct chart_req rq;
    static int32_t vals[GRAPH_POINTS];
    for (;;) {
        if (xQueueReceive(s_chart_q, &rq, portMAX_DELAY) != pdTRUE) continue;
        // snapshot the request target under the lock (id + graph specs) so the unlocked
        // fetch can't tear against a concurrent close/reuse
        char id[40]; struct gspec g[MAX_GRAPHS]; int ngraph = 0;
        if (lvgl_port_lock(0)) {
            struct expand_ref *x = &s_exp[rq.slot];
            if (x->active && x->epoch == rq.epoch) {
                snprintf(id, sizeof(id), "%s", x->id);
                ngraph = x->ngraph;
                for (int i = 0; i < ngraph; i++) g[i] = x->g[i];
            }
            lvgl_port_unlock();
        }
        static double raw[GRAPH_POINTS];
        for (int i = 0; i < ngraph; i++) {
            int n = 0; int32_t vmin = 0, vmax = 0;
            // Local-first: the resolution-selected vmean series from the SD rung replica. Falls back
            // to the BFF readings endpoint when the replica isn't present yet or has no rows for this
            // device/metric (cold-start window + resilience). disp_val handles °C→°F for temp charts.
            int m = ha_replica_rung_query(id, g[i].key, GRAPH_HOURS, raw, GRAPH_POINTS);
            if (m > 0) {
                for (int k = 0; k < m; k++) {
                    int32_t sv = (int32_t)(disp_val(g[i].unit, raw[k]) * CHART_SCALE);
                    vals[k] = sv;
                    if (k == 0 || sv < vmin) vmin = sv;
                    if (k == 0 || sv > vmax) vmax = sv;
                }
                n = m;
            } else {
                char url[384];
                snprintf(url, sizeof(url), "%s/devices/%s/readings?metric=%s&hours=%d&limit=%d",
                         ui_http_base(), id, g[i].key, GRAPH_HOURS, GRAPH_POINTS);
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
                            int32_t sv = (int32_t)(disp_val(g[i].unit, v->valuedouble) * CHART_SCALE);   // °C→°F for temp charts
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
            ESP_LOGI(TAG, "chart %s <- %s (%d pts)", g[i].key, m > 0 ? "rung(SD)" : "http", n);
            if (!chart_fill(rq.slot, rq.epoch, i, vals, n, vmin, vmax))
                break;   // expansion closed mid-fetch — stop working this request
        }
    }
}

void ui_chart_request(int slot, uint32_t epoch)
{
    if (!s_chart_q) return;
    struct chart_req rq = { .slot = slot, .epoch = epoch };
    xQueueSend(s_chart_q, &rq, 0);
}

void ui_chart_start(void)
{
    s_chart_q = xQueueCreate(4, sizeof(struct chart_req));   // expansion chart fetches
    // 16 KB stack: chart_worker now opens sqlite (ha_replica_rung_query) on this task (Phase 1b).
    xTaskCreate(chart_worker, "uichart", 16384, NULL, 4, NULL);
}
