// Sleep-mode wake overlay — see ui_sleep.h. A full-screen dark scrim + one centered "Wake" button on
// lv_layer_top. Pure UI: it knows nothing about scenes or the backlight; the composition root shows/
// hides it off the scene-change hook and wires the Wake tap to the device-local brighten-to-Home.
#include "ui/ui_sleep.h"
#include <stddef.h>
#include "esp_lvgl_port.h"

static lv_obj_t *s_overlay, *s_wake_btn;
static bool s_visible;
static void (*s_on_wake)(void);

void ui_sleep_set_on_wake(void (*cb)(void)) { s_on_wake = cb; }
bool ui_sleep_visible(void) { return s_visible; }

// Wake tap (LVGL click ctx): hide the overlay, then hand off to the composition root's local-wake.
static void wake_cb(lv_event_t *e)
{
    (void)e;
    ui_sleep_hide();
    if (s_on_wake) s_on_wake();
}

void ui_sleep_init(void)
{
    if (!lvgl_port_lock(0)) return;
    lv_obj_t *top = lv_layer_top();                 // floats above the dashboard; survives screen rebuilds
    s_overlay = lv_obj_create(top);
    lv_obj_remove_style_all(s_overlay);
    lv_obj_set_size(s_overlay, lv_pct(100), lv_pct(100));
    lv_obj_set_style_bg_color(s_overlay, lv_color_hex(0x000000), 0);
    lv_obj_set_style_bg_opa(s_overlay, LV_OPA_COVER, 0);   // solid black: the dashboard reads as "asleep"
    lv_obj_clear_flag(s_overlay, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_overlay, LV_OBJ_FLAG_CLICKABLE);      // swallow stray taps so nothing behind reacts

    // One dark, low-contrast "Wake" button, centered. Legible at the faint Sleep backlight but not so
    // bright it lights the room — a soft affordance, distinct from the physical button's true power-off.
    s_wake_btn = lv_button_create(s_overlay);
    lv_obj_set_size(s_wake_btn, 220, 120);
    lv_obj_center(s_wake_btn);
    lv_obj_set_style_bg_color(s_wake_btn, lv_color_hex(0x1e293b), 0);   // dark slate
    lv_obj_set_style_radius(s_wake_btn, 16, 0);
    lv_obj_set_style_border_width(s_wake_btn, 1, 0);
    lv_obj_set_style_border_color(s_wake_btn, lv_color_hex(0x475569), 0);
    lv_obj_add_event_cb(s_wake_btn, wake_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *l = lv_label_create(s_wake_btn);
    lv_label_set_text(l, "Wake");
    lv_obj_set_style_text_font(l, &lv_font_montserrat_28, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(0x94a3b8), 0);          // muted slate — readable, not bright
    lv_obj_center(l);

    lv_obj_add_flag(s_overlay, LV_OBJ_FLAG_HIDDEN);   // hidden until the house enters Sleep
    s_visible = false;
    lvgl_port_unlock();
}

void ui_sleep_show(void)
{
    if (!s_overlay) return;
    if (!lvgl_port_lock(0)) return;
    lv_obj_clear_flag(s_overlay, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(s_overlay);
    s_visible = true;
    lvgl_port_unlock();
}

void ui_sleep_hide(void)
{
    if (!s_overlay) return;
    if (!lvgl_port_lock(0)) return;
    lv_obj_add_flag(s_overlay, LV_OBJ_FLAG_HIDDEN);
    s_visible = false;
    lvgl_port_unlock();
}
