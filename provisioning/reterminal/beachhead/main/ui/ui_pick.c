// Pick-one menu (see ui_pick.h). Built on first open on lv_layer_top() and kept; the card's buttons are
// rebuilt on each open. Fonts compiled: montserrat 14/20/28 only.
#include "ui/ui_pick.h"
#include "ui/ui_format.h"   // ascii_fold
#include <stdio.h>
#include <stdint.h>
#include "esp_log.h"
#include "lvgl.h"

static const char *TAG = "ui.pick";

#define C_CARD  0x111834
#define C_TEXT  0xffffff
#define C_BTN   0x16204a
#define C_EDGE  0x2f7e7a
#define C_CANCEL 0x1e293b

static lv_obj_t *s_back;            // full-screen dimmed backdrop (tap = cancel)
static lv_obj_t *s_card;            // the menu itself (rebuilt per open)
static void (*s_cb)(int index);

void ui_pick_close(void)
{
    if (s_back) lv_obj_add_flag(s_back, LV_OBJ_FLAG_HIDDEN);
    s_cb = NULL;
}

static void on_backdrop(lv_event_t *e)
{
    if (lv_event_get_target(e) != s_back) return;   // a tap inside the card isn't a cancel
    ui_pick_close();
}

static void on_cancel(lv_event_t *e) { (void)e; ui_pick_close(); }

static void on_option(lv_event_t *e)
{
    int i = (int)(intptr_t)lv_event_get_user_data(e);
    void (*cb)(int) = s_cb;
    ui_pick_close();                                 // close first: the callback may navigate away
    if (cb) cb(i);
}

static void option_btn(const char *txt, uint32_t bg, lv_event_cb_t fn, void *ud)
{
    lv_obj_t *b = lv_obj_create(s_card);
    lv_obj_set_size(b, lv_pct(100), 64);
    lv_obj_set_style_bg_color(b, lv_color_hex(bg), 0);
    lv_obj_set_style_border_color(b, lv_color_hex(C_EDGE), 0);
    lv_obj_set_style_border_width(b, 2, 0);
    lv_obj_set_style_radius(b, 10, 0);
    lv_obj_set_style_pad_all(b, 0, 0);
    lv_obj_clear_flag(b, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(b, fn, LV_EVENT_CLICKED, ud);
    lv_obj_t *l = lv_label_create(b);
    lv_label_set_text(l, txt);
    lv_obj_set_style_text_font(l, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(C_TEXT), 0);
    lv_obj_center(l);
}

void ui_pick_open(const char *title, const char *const *labels, int n, void (*cb)(int index))
{
    if (!s_back) {
        s_back = lv_obj_create(lv_layer_top());
        lv_obj_set_size(s_back, lv_pct(100), lv_pct(100));
        lv_obj_set_style_bg_color(s_back, lv_color_hex(0x000000), 0);
        lv_obj_set_style_bg_opa(s_back, LV_OPA_60, 0);
        lv_obj_set_style_border_width(s_back, 0, 0);
        lv_obj_set_style_radius(s_back, 0, 0);
        lv_obj_clear_flag(s_back, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(s_back, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(s_back, on_backdrop, LV_EVENT_CLICKED, NULL);

        s_card = lv_obj_create(s_back);
        lv_obj_set_width(s_card, 520);
        lv_obj_set_height(s_card, LV_SIZE_CONTENT);
        lv_obj_set_style_bg_color(s_card, lv_color_hex(C_CARD), 0);
        lv_obj_set_style_border_color(s_card, lv_color_hex(C_EDGE), 0);
        lv_obj_set_style_border_width(s_card, 2, 0);
        lv_obj_set_style_radius(s_card, 14, 0);
        lv_obj_set_style_pad_all(s_card, 20, 0);
        lv_obj_set_style_pad_row(s_card, 12, 0);
        lv_obj_set_flex_flow(s_card, LV_FLEX_FLOW_COLUMN);
        lv_obj_clear_flag(s_card, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_center(s_card);
    }
    lv_obj_clean(s_card);
    s_cb = cb;

    char buf[64];
    lv_obj_t *t = lv_label_create(s_card);
    ascii_fold(title ? title : "", buf, sizeof buf);
    lv_label_set_text(t, buf);
    lv_label_set_long_mode(t, LV_LABEL_LONG_DOT);
    lv_obj_set_width(t, lv_pct(100));
    lv_obj_set_style_text_font(t, &lv_font_montserrat_28, 0);
    lv_obj_set_style_text_color(t, lv_color_hex(C_TEXT), 0);
    lv_obj_set_style_pad_bottom(t, 4, 0);

    if (n > UI_PICK_MAX) { ESP_LOGW(TAG, "%d options, showing the first %d", n, UI_PICK_MAX); n = UI_PICK_MAX; }
    for (int i = 0; i < n; i++) {
        snprintf(buf, sizeof buf, LV_SYMBOL_IMAGE "  %s", labels[i] ? labels[i] : "?");   // label copied by LVGL
        option_btn(buf, C_BTN, on_option, (void *)(intptr_t)i);
    }
    option_btn(LV_SYMBOL_CLOSE "  Cancel", C_CANCEL, on_cancel, NULL);

    lv_obj_clear_flag(s_back, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(s_back);
    ESP_LOGI(TAG, "open \"%s\" (%d options)", title ? title : "", n);
}
