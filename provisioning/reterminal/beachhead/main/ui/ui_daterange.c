// Custom date range picker (see ui_daterange.h). Built on first open on lv_layer_top() and kept; the
// rollers are re-preset on each open. Same look as ui_pick. Fonts compiled: montserrat 14/20/28 only.
#include "ui/ui_daterange.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_log.h"
#include "lvgl.h"

static const char *TAG = "ui.range";

#define C_CARD   0x111834
#define C_TEXT   0xffffff
#define C_BTN    0x16204a
#define C_EDGE   0x2f7e7a
#define C_CANCEL 0x1e293b
#define C_ERR    0xf87171
#define C_DIM    0x94a3b8

static lv_obj_t *s_back, *s_err;
static lv_obj_t *s_sday, *s_shour, *s_eday, *s_ehour;
static void (*s_cb)(time_t, time_t);
static time_t s_midnight;           // local midnight today, as of the open

static const char *WDAY[] = {"Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"};

// local midnight `d` days before today (mktime normalizes, so DST days come out right)
static time_t day_start(int d)
{
    struct tm lt;
    localtime_r(&s_midnight, &lt);
    lt.tm_mday -= d;
    lt.tm_hour = lt.tm_min = lt.tm_sec = 0;
    lt.tm_isdst = -1;
    return mktime(&lt);
}

static time_t picked(lv_obj_t *day, lv_obj_t *hour)
{
    struct tm lt;
    time_t d = day_start((int)lv_roller_get_selected(day));
    localtime_r(&d, &lt);
    lt.tm_hour = (int)lv_roller_get_selected(hour);
    lt.tm_isdst = -1;
    return mktime(&lt);
}

// preset a day+hour roller pair to `t` (clamped to the roller's span)
static void preset(lv_obj_t *day, lv_obj_t *hour, time_t t)
{
    int d = 0;
    while (d < UI_DATERANGE_DAYS - 1 && t < day_start(d)) d++;
    struct tm lt;
    localtime_r(&t, &lt);
    lv_roller_set_selected(day, d, LV_ANIM_OFF);
    lv_roller_set_selected(hour, lt.tm_hour, LV_ANIM_OFF);
}

static void close_it(void)
{
    if (s_back) lv_obj_add_flag(s_back, LV_OBJ_FLAG_HIDDEN);
    s_cb = NULL;
}

static void on_backdrop(lv_event_t *e) { if (lv_event_get_target(e) == s_back) close_it(); }
static void on_cancel(lv_event_t *e)   { (void)e; close_it(); }

static void on_apply(lv_event_t *e)
{
    (void)e;
    time_t t0 = picked(s_sday, s_shour), t1 = picked(s_eday, s_ehour), now = time(NULL);
    if (t1 + 3600 > now) t1 = now;          // the current hour (or later) means "until now"
    if (t1 <= t0) { lv_label_set_text(s_err, "End must be after Start"); return; }
    void (*cb)(time_t, time_t) = s_cb;
    ESP_LOGI(TAG, "custom range %ld .. %ld (%ld h)", (long)t0, (long)t1, (long)((t1 - t0) / 3600));
    close_it();
    if (cb) cb(t0, t1);
}

static lv_obj_t *roller(lv_obj_t *parent, const char *opts, int w)
{
    lv_obj_t *r = lv_roller_create(parent);
    lv_roller_set_options(r, opts, LV_ROLLER_MODE_NORMAL);
    lv_roller_set_visible_row_count(r, 3);
    lv_obj_set_width(r, w);
    lv_obj_set_style_text_font(r, &lv_font_montserrat_20, 0);
    lv_obj_set_style_bg_color(r, lv_color_hex(C_BTN), 0);
    lv_obj_set_style_text_color(r, lv_color_hex(C_TEXT), 0);
    lv_obj_set_style_border_color(r, lv_color_hex(C_EDGE), 0);
    lv_obj_set_style_bg_color(r, lv_color_hex(C_EDGE), LV_PART_SELECTED);
    return r;
}

static lv_obj_t *row(lv_obj_t *parent, const char *title)
{
    lv_obj_t *r = lv_obj_create(parent);
    lv_obj_remove_style_all(r);
    lv_obj_set_size(r, lv_pct(100), LV_SIZE_CONTENT);
    lv_obj_set_style_pad_column(r, 12, 0);
    lv_obj_set_flex_flow(r, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(r, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    if (title) {
        lv_obj_t *l = lv_label_create(r);
        lv_label_set_text(l, title);
        lv_obj_set_width(l, 80);
        lv_obj_set_style_text_font(l, &lv_font_montserrat_20, 0);
        lv_obj_set_style_text_color(l, lv_color_hex(C_DIM), 0);
    }
    return r;
}

static void button(lv_obj_t *parent, const char *txt, uint32_t bg, lv_event_cb_t cb)
{
    lv_obj_t *b = lv_obj_create(parent);
    lv_obj_set_size(b, 200, 60);
    lv_obj_set_style_bg_color(b, lv_color_hex(bg), 0);
    lv_obj_set_style_border_color(b, lv_color_hex(C_EDGE), 0);
    lv_obj_set_style_border_width(b, 2, 0);
    lv_obj_set_style_radius(b, 10, 0);
    lv_obj_set_style_pad_all(b, 0, 0);
    lv_obj_clear_flag(b, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *l = lv_label_create(b);
    lv_label_set_text(l, txt);
    lv_obj_set_style_text_font(l, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(C_TEXT), 0);
    lv_obj_center(l);
}

// day options as of today ("Today\nYesterday\nTue 10/28\n..."), into the two day rollers
static void set_day_options(void)
{
    size_t cap = UI_DATERANGE_DAYS * 14;
    char *o = malloc(cap);
    if (!o) return;
    size_t n = 0;
    for (int d = 0; d < UI_DATERANGE_DAYS && n < cap - 16; d++) {
        time_t t = day_start(d);
        struct tm lt;
        localtime_r(&t, &lt);
        if (d == 0)      n += snprintf(o + n, cap - n, "Today");
        else if (d == 1) n += snprintf(o + n, cap - n, "\nYesterday");
        else             n += snprintf(o + n, cap - n, "\n%s %d/%d", WDAY[lt.tm_wday], lt.tm_mon + 1, lt.tm_mday);
    }
    lv_roller_set_options(s_sday, o, LV_ROLLER_MODE_NORMAL);   // copies the string
    lv_roller_set_options(s_eday, o, LV_ROLLER_MODE_NORMAL);
    free(o);
}

static void build(void)
{
    s_back = lv_obj_create(lv_layer_top());
    lv_obj_set_size(s_back, lv_pct(100), lv_pct(100));
    lv_obj_set_style_bg_color(s_back, lv_color_hex(0x000000), 0);
    lv_obj_set_style_bg_opa(s_back, LV_OPA_60, 0);
    lv_obj_set_style_border_width(s_back, 0, 0);
    lv_obj_set_style_radius(s_back, 0, 0);
    lv_obj_clear_flag(s_back, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_back, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(s_back, on_backdrop, LV_EVENT_CLICKED, NULL);

    lv_obj_t *card = lv_obj_create(s_back);
    lv_obj_set_width(card, 600);
    lv_obj_set_height(card, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_color(card, lv_color_hex(C_CARD), 0);
    lv_obj_set_style_border_color(card, lv_color_hex(C_EDGE), 0);
    lv_obj_set_style_border_width(card, 2, 0);
    lv_obj_set_style_radius(card, 14, 0);
    lv_obj_set_style_pad_all(card, 20, 0);
    lv_obj_set_style_pad_row(card, 14, 0);
    lv_obj_set_flex_flow(card, LV_FLEX_FLOW_COLUMN);
    lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_center(card);

    lv_obj_t *t = lv_label_create(card);
    lv_label_set_text(t, "Custom range");
    lv_obj_set_style_text_font(t, &lv_font_montserrat_28, 0);
    lv_obj_set_style_text_color(t, lv_color_hex(C_TEXT), 0);

    static char hours[24 * 6];
    size_t n = 0;
    for (int h = 0; h < 24; h++) n += snprintf(hours + n, sizeof hours - n, "%s%02d:00", h ? "\n" : "", h);

    lv_obj_t *rs = row(card, "Start");
    s_sday = roller(rs, "Today", 240);
    s_shour = roller(rs, hours, 140);
    lv_obj_t *re = row(card, "End");
    s_eday = roller(re, "Today", 240);
    s_ehour = roller(re, hours, 140);

    s_err = lv_label_create(card);
    lv_label_set_text(s_err, "");
    lv_obj_set_style_text_font(s_err, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(s_err, lv_color_hex(C_ERR), 0);

    lv_obj_t *rb = row(card, NULL);
    button(rb, LV_SYMBOL_OK "  Apply", C_BTN, on_apply);
    button(rb, LV_SYMBOL_CLOSE "  Cancel", C_CANCEL, on_cancel);
}

void ui_daterange_open(time_t t0, time_t t1, void (*cb)(time_t t0, time_t t1))
{
    if (!s_back) build();
    time_t now = time(NULL);
    struct tm lt;
    localtime_r(&now, &lt);
    lt.tm_hour = lt.tm_min = lt.tm_sec = 0;
    lt.tm_isdst = -1;
    s_midnight = mktime(&lt);
    set_day_options();                       // "Today" moves at midnight, so rebuilt per open
    preset(s_sday, s_shour, t0);
    preset(s_eday, s_ehour, t1);
    lv_label_set_text(s_err, "");
    s_cb = cb;
    lv_obj_clear_flag(s_back, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(s_back);
}
