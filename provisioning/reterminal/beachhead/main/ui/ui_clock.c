// Top-bar wall clock (see ui_clock.h). Reads the system clock every second; the backbone in
// beachhead_main.c keeps it set (RTC holdover + SNTP). Extracted as its own ui/ unit (ADR-0020).
#include "ui/ui_clock.h"
#include <time.h>

static lv_obj_t *s_lbl;

static void clock_tick(lv_timer_t *t)
{
    (void)t;
    time_t now = time(NULL);
    struct tm lt;
    localtime_r(&now, &lt);
    if (lt.tm_year + 1900 < 2020) {           // time not set yet (RTC invalid + no SNTP) → don't lie
        lv_label_set_text(s_lbl, "--:--");
        return;
    }
    char buf[8];
    strftime(buf, sizeof buf, "%H:%M", &lt);
    lv_label_set_text(s_lbl, buf);
}

void ui_clock_init(lv_obj_t *topbar)
{
    s_lbl = lv_label_create(topbar);
    lv_obj_set_style_text_font(s_lbl, &lv_font_montserrat_28, 0);
    lv_obj_set_style_text_color(s_lbl, lv_color_hex(0xffffff), 0);
    clock_tick(NULL);                          // initial paint
    lv_timer_create(clock_tick, 1000, NULL);   // 1 Hz; runs in the LVGL task (holds the lock)
}
