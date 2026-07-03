// Top-bar power-off button + confirm dialog (see ui_power.h). NOT admin-gated by design:
// physical access == permission to power the device down.
//
// Two-phase, never-freezes: tapping the ⏻ button opens an "ask" overlay [Power off][Cancel].
// Committing calls the NON-BLOCKING bsp_power_off() (drops the PWR_HOLD rail latch). On battery
// the rail dies there and then. While USB holds the device up, the overlay swaps to an "unplug
// USB to finish" message whose Cancel RE-ARMS the rail (bsp_power_rearm) and resumes — so the UI
// is always responsive and recoverable, even USB-powered on the bench.
#include "ui/ui_power.h"
#include "bsp_display.h"    // bsp_power_off() / bsp_power_rearm()
#include "lvgl.h"

static lv_obj_t *s_confirm_ov;   // full-screen confirm overlay (top layer, hidden until asked)
static lv_obj_t *s_msg, *s_sub;  // headline + sub text (swapped between ask / committed states)
static lv_obj_t *s_ok;           // the "Power off" commit button (hidden after commit)
static bool s_committed;         // true once PWR_HOLD released while USB still powers us

// Reset the overlay to the initial "ask" state: "Power off?" with the commit button shown.
static void set_ask_state(void)
{
    lv_label_set_text(s_msg, LV_SYMBOL_POWER "  Power off the device?");
    lv_label_set_text(s_sub, "Turn it back on with the physical side button.");
    lv_obj_clear_flag(s_ok, LV_OBJ_FLAG_HIDDEN);
}

// "Power off" pressed: release the rail (non-blocking). On battery this never returns. On USB it
// returns and we leave a terminal "unplug to finish" message up; Cancel then re-arms & resumes.
static void confirm_cb(lv_event_t *e)
{
    (void)e;
    lv_label_set_text(s_msg, LV_SYMBOL_POWER "  Powering off");
    lv_label_set_text(s_sub, "Unplug USB to finish.  (Still powered over USB — tap Cancel to resume.)");
    lv_obj_add_flag(s_ok, LV_OBJ_FLAG_HIDDEN);   // no second commit; Cancel now means re-arm
    s_committed = true;
    bsp_power_off();
}

// Cancel: if we already committed (USB still up), re-arm the rail so unplugging won't kill it.
// Always dismiss the overlay and reset it to the ask-state for next time.
static void cancel_cb(lv_event_t *e)
{
    (void)e;
    if (s_committed) { bsp_power_rearm(); s_committed = false; }
    set_ask_state();
    lv_obj_add_flag(s_confirm_ov, LV_OBJ_FLAG_HIDDEN);
}

// Top-bar ⏻ button -> (re)open the confirm overlay in the ask state.
static void power_btn_cb(lv_event_t *e)
{
    (void)e;
    set_ask_state();
    lv_obj_clear_flag(s_confirm_ov, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(s_confirm_ov);
}

void ui_power_init(lv_obj_t *topbar)
{
    lv_obj_t *btn = lv_button_create(topbar);
    lv_obj_set_height(btn, 46);
    lv_obj_set_style_bg_color(btn, lv_color_hex(0x7f1d1d), 0);   // dark red
    lv_obj_add_event_cb(btn, power_btn_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *lbl = lv_label_create(btn);
    lv_label_set_text(lbl, LV_SYMBOL_POWER);
    lv_obj_set_style_text_font(lbl, &lv_font_montserrat_20, 0);
    lv_obj_center(lbl);

    // confirm overlay (top layer, hidden until the power button is tapped)
    s_confirm_ov = lv_obj_create(lv_layer_top());
    lv_obj_set_size(s_confirm_ov, lv_pct(100), lv_pct(100));
    lv_obj_set_style_bg_color(s_confirm_ov, lv_color_hex(0x0b1021), 0);
    lv_obj_set_style_bg_opa(s_confirm_ov, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(s_confirm_ov, 0, 0);
    lv_obj_set_flex_flow(s_confirm_ov, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(s_confirm_ov, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(s_confirm_ov, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_confirm_ov, LV_OBJ_FLAG_HIDDEN);

    s_msg = lv_label_create(s_confirm_ov);
    lv_obj_set_style_text_font(s_msg, &lv_font_montserrat_28, 0);
    lv_obj_set_style_text_color(s_msg, lv_color_hex(0xffffff), 0);
    lv_obj_set_style_pad_bottom(s_msg, 6, 0);

    s_sub = lv_label_create(s_confirm_ov);
    lv_obj_set_style_text_color(s_sub, lv_color_hex(0x94a3b8), 0);
    lv_obj_set_style_text_align(s_sub, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_pad_bottom(s_sub, 24, 0);

    lv_obj_t *row = lv_obj_create(s_confirm_ov);
    lv_obj_set_size(row, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(row, 0, 0);
    lv_obj_set_style_border_width(row, 0, 0);
    lv_obj_set_style_pad_all(row, 0, 0);
    lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
    lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *cancel = lv_button_create(row);
    lv_obj_set_size(cancel, 200, 72);
    lv_obj_set_style_bg_color(cancel, lv_color_hex(0x334155), 0);
    lv_obj_set_style_margin_right(cancel, 16, 0);
    lv_obj_add_event_cb(cancel, cancel_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *cl = lv_label_create(cancel);
    lv_label_set_text(cl, LV_SYMBOL_CLOSE "  Cancel");
    lv_obj_set_style_text_font(cl, &lv_font_montserrat_20, 0);
    lv_obj_center(cl);

    s_ok = lv_button_create(row);
    lv_obj_set_size(s_ok, 220, 72);
    lv_obj_set_style_bg_color(s_ok, lv_color_hex(0xb91c1c), 0);   // red
    lv_obj_add_event_cb(s_ok, confirm_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *ol = lv_label_create(s_ok);
    lv_label_set_text(ol, LV_SYMBOL_POWER "  Power off");
    lv_obj_set_style_text_font(ol, &lv_font_montserrat_20, 0);
    lv_obj_center(ol);

    set_ask_state();
}
