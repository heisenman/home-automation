// Graph time axis (see ui_graph_axis.h). Fonts compiled: montserrat 14/20/28 only.
#include "ui/ui_graph_axis.h"
#include <stdio.h>
#include <string.h>
#include "esp_log.h"

static const char *TAG = "ui.gaxis";

#define MAX_HOURS   (24 * 92)       // hour boundaries walked (a 90-day custom window + slack)
#define MAX_LABELS  64
#define LABEL_W     64              // fixed label box, centered on its tick
#define C_DAY       0x334155
#define C_HOUR      0x475569
#define C_LABEL     0x94a3b8

struct tick { time_t t; char text[16]; };

static lv_obj_t *vline(lv_obj_t *layer, int x, int y, int h, uint32_t color)
{
    lv_obj_t *o = lv_obj_create(layer);
    lv_obj_remove_style_all(o);
    lv_obj_set_pos(o, x, y);
    lv_obj_set_size(o, 1, h);
    lv_obj_set_style_bg_color(o, lv_color_hex(color), 0);
    lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
    lv_obj_clear_flag(o, LV_OBJ_FLAG_CLICKABLE);
    return o;
}

void ui_graph_axis_draw(lv_obj_t *layer, lv_obj_t *xrow, time_t t0, time_t t1)
{
    lv_obj_clean(layer);
    lv_obj_clean(xrow);
    if (t1 <= t0) return;
    lv_obj_update_layout(layer);
    int w = lv_obj_get_width(layer), h = lv_obj_get_height(layer);
    if (w < 2 || h < 2) return;
    double span = (double)(t1 - t0), span_h = span / 3600.0;
#define XOF(t) ((int)((double)((t) - t0) * (w - 1) / span))

    // hour boundaries: whole-hour zones (America/Los_Angeles) put local hours on UTC hour boundaries
    static struct tick days[MAX_LABELS], hours[MAX_LABELS * 8], ticks[MAX_LABELS * 9];
    int nd = 0, nh = 0, walked = 0;
    time_t t = t0 - (t0 % 3600);
    if (t < t0) t += 3600;
    for (; t <= t1 && walked < MAX_HOURS; t += 3600, walked++) {
        struct tm lt;
        localtime_r(&t, &lt);
        if (lt.tm_hour == 0) {
            vline(layer, XOF(t), 0, h, C_DAY);                       // day grid line
            if (nd < MAX_LABELS) {
                days[nd].t = t;
                snprintf(days[nd].text, sizeof days[nd].text, "%d/%d", lt.tm_mon + 1, lt.tm_mday);
                nd++;
            }
        } else {
            if (span_h <= 7 * 24) vline(layer, XOF(t), h - 10, 10, C_HOUR);   // minor hour tick
            if (nh < (int)(sizeof hours / sizeof hours[0])) {
                hours[nh].t = t;
                snprintf(hours[nh].text, sizeof hours[nh].text, "%02d:00", lt.tm_hour);
                nh++;
            }
        }
    }
    if (walked >= MAX_HOURS) ESP_LOGW(TAG, "window longer than %d h: grid stops there", MAX_HOURS);

    // labels: the days; under 2 midnights, also the hour ticks on the finest step that keeps ~7
    int nt = 0;
    for (int i = 0; i < nd; i++) ticks[nt++] = days[i];
    if (nd < 2) {
        static const int STEPS[] = {1, 2, 3, 6, 12};
        int step = 12;
        for (int i = 0; i < 5; i++) if (span_h / STEPS[i] <= 7) { step = STEPS[i]; break; }
        for (int i = 0; i < nh && nt < (int)(sizeof ticks / sizeof ticks[0]); i++) {
            struct tm lt;
            localtime_r(&hours[i].t, &lt);
            if (lt.tm_hour % step == 0) ticks[nt++] = hours[i];
        }
        for (int i = 1; i < nt; i++)                                  // insertion sort by time
            for (int j = i; j > 0 && ticks[j - 1].t > ticks[j].t; j--) {
                struct tick tmp = ticks[j]; ticks[j] = ticks[j - 1]; ticks[j - 1] = tmp;
            }
    }
    int every = (nt + 6) / 7;
    if (every < 1) every = 1;
    lv_obj_update_layout(xrow);
    int xw = lv_obj_get_width(xrow);
    for (int i = 0; i < nt; i += every) {
        lv_obj_t *l = lv_label_create(xrow);
        lv_label_set_text(l, ticks[i].text);
        lv_obj_set_width(l, LABEL_W);
        lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
        lv_obj_set_style_text_font(l, &lv_font_montserrat_14, 0);
        lv_obj_set_style_text_color(l, lv_color_hex(C_LABEL), 0);
        int x = XOF(ticks[i].t) - LABEL_W / 2;
        if (x < 0) x = 0;
        if (x > xw - LABEL_W) x = xw - LABEL_W;
        lv_obj_set_pos(l, x, 0);
    }
#undef XOF
}
