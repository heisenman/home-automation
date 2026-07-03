// Actuator controls (see ui_controls.h). Extracted from ui_tiles.c verbatim (ADR-0020 module-first);
// behavior identical. actuator_card -> ui_controls_add_card(d, grid) (renders into the shared grid);
// override/policy edits now go through ui_admin_set_override/ui_admin_set_policy instead of building
// struct admin_req directly, and act_clicked_cb reads ui_admin_active() for the admin-controls gate.
#include "ui/ui_controls.h"
#include "ui/ui_admin.h"    // active / set_override / set_policy
#include "ui/ui_http.h"     // base + post_cmd
#include <string.h>
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include "lvgl.h"
#include "esp_lvgl_port.h"
#include "cJSON.h"
#include "secrets.h"        // PANEL_TOKEN (operator JWT); "" => read-only, no control buttons

#define HAVE_TOKEN (sizeof(PANEL_TOKEN) > 1)

static const char *TAG = "ui.controls";

// --- command path (touch an actuator -> POST /devices/<id>/command with the operator token) ---
struct cmd_req { char id[40]; char body[176]; };
static QueueHandle_t s_cmd_q;      // command requests -> cmd_worker (HTTP POST off the LVGL stack)
struct act_ref {
    char id[40]; char name[40]; bool running;
    // current automation policy (from /api/v1/displays), seeds the admin editor
    bool has_policy, enabled; char strategy[16]; double on_above, off_below; bool has_override;
};
#define MAX_ACTS 12
static struct act_ref s_acts[MAX_ACTS];
static int s_nacts;
static lv_obj_t *s_cmd_ov, *s_cmd_title, *s_cmd_result;   // actuator command overlay
static char s_cmd_target[40];      // device_id the overlay currently controls
// admin-only controls inside the overlay (shown only when unlocked): override + automation editor
static lv_obj_t *s_admin_ctrls, *s_ov_onabove, *s_ov_offbelow, *s_ov_autorow;
static double s_edit_on_above, s_edit_off_below;   // live-edited policy values
static bool s_ov_has_policy;

static void cmd_worker(void *pv)
{
    struct cmd_req req;
    for (;;) {
        if (xQueueReceive(s_cmd_q, &req, portMAX_DELAY) != pdTRUE) continue;
        char url[288];
        snprintf(url, sizeof(url), "%s/devices/%s/command", ui_http_base(), req.id);
        int code = ui_http_post_cmd(url, req.body);
        ESP_LOGI(TAG, "cmd %s %s -> HTTP %d", req.id, req.body, code);
        if (lvgl_port_lock(0)) {
            if (s_cmd_result) {
                if (code >= 200 && code < 300) lv_label_set_text(s_cmd_result, "command accepted");
                else { char r[40]; snprintf(r, sizeof(r), "rejected (HTTP %d)", code); lv_label_set_text(s_cmd_result, r); }
            }
            lvgl_port_unlock();
        }
    }
}

// Enqueue a switchable on/off for the overlay's current target (runs in LVGL ctx).
static void cmd_send_switch(bool on)
{
    if (!s_cmd_q || !s_cmd_target[0]) return;
    struct cmd_req req;
    snprintf(req.id, sizeof(req.id), "%s", s_cmd_target);
    snprintf(req.body, sizeof(req.body),
             "{\"trait\":\"switchable\",\"action\":\"set\",\"args\":{\"on\":%s}}", on ? "true" : "false");
    if (xQueueSend(s_cmd_q, &req, 0) == pdTRUE && s_cmd_result)
        lv_label_set_text(s_cmd_result, on ? "turning ON..." : "turning OFF...");
}
static void cmd_on_cb(lv_event_t *e)   { (void)e; cmd_send_switch(true); }
static void cmd_off_cb(lv_event_t *e)  { (void)e; cmd_send_switch(false); }
static void cmd_close_cb(lv_event_t *e){ (void)e; lv_obj_add_flag(s_cmd_ov, LV_OBJ_FLAG_HIDDEN); }

// ---- admin actuator controls: manual override + automation editor (ADR-0019 Phase C) ----
// Override: force the actuator off/boost for a window, or clear back to auto (POST /control/{id}/override).
static void ov_send_override(const char *action, int duration_min)
{
    if (!s_cmd_target[0]) return;
    char body[96];
    if (duration_min > 0)
        snprintf(body, sizeof(body), "{\"action\":\"%s\",\"duration_min\":%d}", action, duration_min);
    else
        snprintf(body, sizeof(body), "{\"action\":\"%s\"}", action);
    ui_admin_set_override(s_cmd_target, body);
    if (s_cmd_result) lv_label_set_text(s_cmd_result, "applying…");
}
static void ov_off1h_cb(lv_event_t *e) { (void)e; ov_send_override("off", 60); }
static void ov_boost_cb(lv_event_t *e) { (void)e; ov_send_override("boost_on", 60); }
static void ov_resume_cb(lv_event_t *e){ (void)e; ov_send_override("clear", 0); }

// Automation editor: on_above / off_below steppers (hysteresis), constrained ON>OFF, PUT /control/{id}/policy.
static void ov_update_auto_labels(void)
{
    char b[24];
    if (s_ov_onabove)  { snprintf(b, sizeof(b), "on > %.0f", s_edit_on_above);  lv_label_set_text(s_ov_onabove, b); }
    if (s_ov_offbelow) { snprintf(b, sizeof(b), "off < %.0f", s_edit_off_below); lv_label_set_text(s_ov_offbelow, b); }
}
static void ov_oa_dn(lv_event_t *e){ (void)e; if (s_edit_on_above > s_edit_off_below + 1) s_edit_on_above -= 1; ov_update_auto_labels(); }
static void ov_oa_up(lv_event_t *e){ (void)e; if (s_edit_on_above < 95) s_edit_on_above += 1; ov_update_auto_labels(); }
static void ov_ob_dn(lv_event_t *e){ (void)e; if (s_edit_off_below > 5) s_edit_off_below -= 1; ov_update_auto_labels(); }
static void ov_ob_up(lv_event_t *e){ (void)e; if (s_edit_off_below < s_edit_on_above - 1) s_edit_off_below += 1; ov_update_auto_labels(); }
static void ov_save_policy_cb(lv_event_t *e)
{
    (void)e;
    if (!s_cmd_target[0]) return;
    char body[160];
    snprintf(body, sizeof(body),
        "{\"enabled\":true,\"control\":{\"strategy\":\"hysteresis\",\"on_above\":%.0f,\"off_below\":%.0f}}",
        s_edit_on_above, s_edit_off_below);
    ui_admin_set_policy(s_cmd_target, body);
    if (s_cmd_result) lv_label_set_text(s_cmd_result, "saving…");
}

static void act_clicked_cb(lv_event_t *e)
{
    int idx = (int)(intptr_t)lv_event_get_user_data(e);
    if (idx < 0 || idx >= s_nacts || !s_cmd_ov) return;
    struct act_ref *ar = &s_acts[idx];
    snprintf(s_cmd_target, sizeof(s_cmd_target), "%s", ar->id);
    lv_label_set_text(s_cmd_title, ar->name);
    if (s_cmd_result) lv_label_set_text(s_cmd_result, ar->running ? "currently ON" : "currently OFF");
    // seed + show the admin controls only when unlocked; the automation editor only for hysteresis devices
    s_ov_has_policy = ar->has_policy;
    s_edit_on_above = ar->on_above; s_edit_off_below = ar->off_below;
    ov_update_auto_labels();
    bool admin = ui_admin_active();
    if (s_admin_ctrls) {
        if (admin) lv_obj_clear_flag(s_admin_ctrls, LV_OBJ_FLAG_HIDDEN);
        else lv_obj_add_flag(s_admin_ctrls, LV_OBJ_FLAG_HIDDEN);
    }
    if (s_ov_autorow) {
        if (admin && s_ov_has_policy) lv_obj_clear_flag(s_ov_autorow, LV_OBJ_FLAG_HIDDEN);
        else lv_obj_add_flag(s_ov_autorow, LV_OBJ_FLAG_HIDDEN);
    }
    lv_obj_clear_flag(s_cmd_ov, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(s_cmd_ov);
}

void ui_controls_reset(void) { s_nacts = 0; }

// Read-only actuator/controllable-device card (from /api/v1/displays). Distinct
// amber accent; green border when running. Tap -> command overlay (if token set).
void ui_controls_add_card(cJSON *d, lv_obj_t *grid)
{
    const cJSON *jid = cJSON_GetObjectItem(d, "device_id");
    const cJSON *jname = cJSON_GetObjectItem(d, "name");
    const cJSON *jroom = cJSON_GetObjectItem(d, "room");
    bool running = cJSON_IsTrue(cJSON_GetObjectItem(d, "running"));
    cJSON *sensor = cJSON_GetObjectItem(d, "sensor");
    cJSON *act = cJSON_GetObjectItem(d, "actuator");
    const char *name = cJSON_IsString(jname) ? jname->valuestring
                     : (cJSON_IsString(jid) ? jid->valuestring : "device");
    const char *room = cJSON_IsString(jroom) ? jroom->valuestring : "";

    lv_obj_t *card = lv_obj_create(grid);
    lv_obj_set_size(card, 236, 158);
    lv_obj_set_style_bg_color(card, lv_color_hex(0x241f10), 0);
    lv_obj_set_style_border_width(card, 2, 0);
    lv_obj_set_style_border_color(card, lv_color_hex(running ? 0x22c55e : 0x3a3a3a), 0);
    lv_obj_set_style_radius(card, 12, 0);
    lv_obj_set_style_pad_all(card, 10, 0);
    lv_obj_set_flex_flow(card, LV_FLEX_FLOW_COLUMN);
    lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *t = lv_label_create(card);
    lv_label_set_text(t, name);
    lv_obj_set_style_text_font(t, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(t, lv_color_hex(0xffffff), 0);
    lv_label_set_long_mode(t, LV_LABEL_LONG_DOT);
    lv_obj_set_width(t, lv_pct(100));

    lv_obj_t *r = lv_label_create(card);
    lv_label_set_text(r, room);
    lv_obj_set_style_text_color(r, lv_color_hex(0xfbbf24), 0);

    lv_obj_t *st = lv_label_create(card);
    lv_label_set_text(st, running ? "ON" : "OFF");
    lv_obj_set_style_text_font(st, &lv_font_montserrat_28, 0);
    lv_obj_set_style_text_color(st, lv_color_hex(running ? 0x22c55e : 0x94a3b8), 0);
    lv_obj_set_style_pad_top(st, 4, 0);

    char line[80] = ""; size_t o = 0;
    cJSON *sv = cJSON_IsObject(sensor) ? cJSON_GetObjectItem(sensor, "value") : NULL;
    if (cJSON_IsNumber(sv)) o += snprintf(line + o, sizeof(line) - o, "now %.0f", sv->valuedouble);
    cJSON *tp = cJSON_IsObject(act) ? cJSON_GetObjectItem(act, "target_pct") : NULL;
    if (cJSON_IsNumber(tp)) o += snprintf(line + o, sizeof(line) - o, "%s-> %.0f", o ? "   " : "", tp->valuedouble);
    if (line[0]) {
        lv_obj_t *l = lv_label_create(card);
        lv_label_set_text(l, line);
        lv_obj_set_style_text_color(l, lv_color_hex(0x94a3b8), 0);
    }

    // register + make tappable for commands (only when we hold an operator token)
    if (HAVE_TOKEN && s_nacts < MAX_ACTS && cJSON_IsString(jid)) {
        int idx = s_nacts;
        struct act_ref *ar = &s_acts[s_nacts++];
        snprintf(ar->id, sizeof(ar->id), "%s", jid->valuestring);
        snprintf(ar->name, sizeof(ar->name), "%s", name);
        ar->running = running;
        // capture current automation policy (seeds the admin editor) + override presence
        ar->has_policy = false; ar->on_above = ar->off_below = 0; ar->enabled = true;
        ar->strategy[0] = 0; ar->has_override = cJSON_IsObject(cJSON_GetObjectItem(d, "override"));
        cJSON *ctrl = cJSON_GetObjectItem(d, "control");
        if (cJSON_IsObject(ctrl)) {
            cJSON *oa = cJSON_GetObjectItem(ctrl, "on_above");
            cJSON *ob = cJSON_GetObjectItem(ctrl, "off_below");
            cJSON *stt = cJSON_GetObjectItem(ctrl, "strategy");
            cJSON *en = cJSON_GetObjectItem(ctrl, "enabled");
            if (cJSON_IsString(stt)) snprintf(ar->strategy, sizeof(ar->strategy), "%s", stt->valuestring);
            if (cJSON_IsBool(en)) ar->enabled = cJSON_IsTrue(en);
            if (cJSON_IsNumber(oa) && cJSON_IsNumber(ob)) {   // hysteresis thresholds -> editable
                ar->on_above = oa->valuedouble; ar->off_below = ob->valuedouble; ar->has_policy = true;
            }
        }
        lv_obj_add_flag(card, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(card, act_clicked_cb, LV_EVENT_CLICKED, (void *)(intptr_t)idx);
    }
}

void ui_controls_init(void)
{
    if (!HAVE_TOKEN) return;   // read-only build: no command overlay, no worker

    // actuator command overlay (top layer, hidden until an actuator card is tapped)
    s_cmd_ov = lv_obj_create(lv_layer_top());
    lv_obj_set_size(s_cmd_ov, lv_pct(100), lv_pct(100));
    lv_obj_set_style_bg_color(s_cmd_ov, lv_color_hex(0x0b1021), 0);
    lv_obj_set_style_bg_opa(s_cmd_ov, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(s_cmd_ov, 0, 0);
    lv_obj_set_style_pad_all(s_cmd_ov, 36, 0);
    lv_obj_set_flex_flow(s_cmd_ov, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(s_cmd_ov, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_add_flag(s_cmd_ov, LV_OBJ_FLAG_HIDDEN);

    s_cmd_title = lv_label_create(s_cmd_ov);
    lv_obj_set_style_text_font(s_cmd_title, &lv_font_montserrat_28, 0);
    lv_obj_set_style_text_color(s_cmd_title, lv_color_hex(0xffffff), 0);
    s_cmd_result = lv_label_create(s_cmd_ov);
    lv_obj_set_style_text_color(s_cmd_result, lv_color_hex(0x8fb4ff), 0);
    lv_obj_set_style_pad_bottom(s_cmd_result, 22, 0);

    // operator controls (always available with the panel token): ON / OFF
    struct { const char *txt; uint32_t col; lv_event_cb_t cb; } ops[] = {
        {"Turn ON",  0x16a34a, cmd_on_cb},
        {"Turn OFF", 0x334155, cmd_off_cb},
    };
    for (unsigned i = 0; i < 2; i++) {
        lv_obj_t *bt = lv_button_create(s_cmd_ov);
        lv_obj_set_size(bt, 340, 64);
        lv_obj_set_style_bg_color(bt, lv_color_hex(ops[i].col), 0);
        lv_obj_set_style_margin_top(bt, i ? 12 : 0, 0);
        lv_obj_add_event_cb(bt, ops[i].cb, LV_EVENT_CLICKED, NULL);
        lv_obj_t *bl = lv_label_create(bt);
        lv_label_set_text(bl, ops[i].txt);
        lv_obj_set_style_text_font(bl, &lv_font_montserrat_20, 0);
        lv_obj_center(bl);
    }

    // ── admin-only controls (hidden unless unlocked): manual override + automation editor ──
    s_admin_ctrls = lv_obj_create(s_cmd_ov);
    lv_obj_set_width(s_admin_ctrls, 600);
    lv_obj_set_height(s_admin_ctrls, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(s_admin_ctrls, 0, 0);
    lv_obj_set_style_border_width(s_admin_ctrls, 0, 0);
    lv_obj_set_style_pad_all(s_admin_ctrls, 0, 0);
    lv_obj_set_style_pad_top(s_admin_ctrls, 16, 0);
    lv_obj_set_style_pad_row(s_admin_ctrls, 10, 0);
    lv_obj_set_flex_flow(s_admin_ctrls, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(s_admin_ctrls, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(s_admin_ctrls, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_admin_ctrls, LV_OBJ_FLAG_HIDDEN);

    lv_obj_t *ovlbl = lv_label_create(s_admin_ctrls);
    lv_label_set_text(ovlbl, "Override");
    lv_obj_set_style_text_color(ovlbl, lv_color_hex(0xfbbf24), 0);

    lv_obj_t *ovrow = lv_obj_create(s_admin_ctrls);
    lv_obj_set_width(ovrow, lv_pct(100));
    lv_obj_set_height(ovrow, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(ovrow, 0, 0);
    lv_obj_set_style_border_width(ovrow, 0, 0);
    lv_obj_set_style_pad_all(ovrow, 0, 0);
    lv_obj_set_style_pad_column(ovrow, 10, 0);
    lv_obj_set_flex_flow(ovrow, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(ovrow, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(ovrow, LV_OBJ_FLAG_SCROLLABLE);
    struct { const char *txt; uint32_t col; lv_event_cb_t cb; } ob[] = {
        {"Off 1h",   0x334155, ov_off1h_cb},
        {"Boost 1h", 0x2563eb, ov_boost_cb},
        {"Resume",   0x1e293b, ov_resume_cb},
    };
    for (unsigned i = 0; i < 3; i++) {
        lv_obj_t *bt = lv_button_create(ovrow);
        lv_obj_set_size(bt, 180, 60);
        lv_obj_set_style_bg_color(bt, lv_color_hex(ob[i].col), 0);
        lv_obj_add_event_cb(bt, ob[i].cb, LV_EVENT_CLICKED, NULL);
        lv_obj_t *bl = lv_label_create(bt);
        lv_label_set_text(bl, ob[i].txt);
        lv_obj_set_style_text_font(bl, &lv_font_montserrat_20, 0);
        lv_obj_center(bl);
    }

    // automation editor (only for hysteresis devices; shown when unlocked)
    s_ov_autorow = lv_obj_create(s_admin_ctrls);
    lv_obj_set_width(s_ov_autorow, lv_pct(100));
    lv_obj_set_height(s_ov_autorow, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(s_ov_autorow, 0, 0);
    lv_obj_set_style_border_width(s_ov_autorow, 0, 0);
    lv_obj_set_style_pad_all(s_ov_autorow, 0, 0);
    lv_obj_set_style_pad_top(s_ov_autorow, 10, 0);
    lv_obj_set_style_pad_row(s_ov_autorow, 8, 0);
    lv_obj_set_flex_flow(s_ov_autorow, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(s_ov_autorow, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(s_ov_autorow, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(s_ov_autorow, LV_OBJ_FLAG_HIDDEN);

    lv_obj_t *autolbl = lv_label_create(s_ov_autorow);
    lv_label_set_text(autolbl, "Automation (humidity %)");
    lv_obj_set_style_text_color(autolbl, lv_color_hex(0xfbbf24), 0);

    struct { lv_obj_t **lbl; lv_event_cb_t dn, up; } steps[] = {
        {&s_ov_onabove,  ov_oa_dn, ov_oa_up},
        {&s_ov_offbelow, ov_ob_dn, ov_ob_up},
    };
    for (unsigned s = 0; s < 2; s++) {
        lv_obj_t *row = lv_obj_create(s_ov_autorow);
        lv_obj_set_width(row, lv_pct(100));
        lv_obj_set_height(row, LV_SIZE_CONTENT);
        lv_obj_set_style_bg_opa(row, 0, 0);
        lv_obj_set_style_border_width(row, 0, 0);
        lv_obj_set_style_pad_all(row, 0, 0);
        lv_obj_set_style_pad_column(row, 12, 0);
        lv_obj_set_flex_flow(row, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(row, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_t *dn = lv_button_create(row);
        lv_obj_set_size(dn, 68, 60);
        lv_obj_set_style_bg_color(dn, lv_color_hex(0x334155), 0);
        lv_obj_add_event_cb(dn, steps[s].dn, LV_EVENT_CLICKED, NULL);
        lv_obj_t *dl = lv_label_create(dn);
        lv_label_set_text(dl, LV_SYMBOL_MINUS);
        lv_obj_set_style_text_font(dl, &lv_font_montserrat_28, 0);
        lv_obj_center(dl);
        lv_obj_t *val = lv_label_create(row);
        lv_obj_set_width(val, 160);
        lv_obj_set_style_text_font(val, &lv_font_montserrat_28, 0);
        lv_obj_set_style_text_color(val, lv_color_hex(0xffffff), 0);
        lv_obj_set_style_text_align(val, LV_TEXT_ALIGN_CENTER, 0);
        lv_label_set_text(val, "-");
        *steps[s].lbl = val;
        lv_obj_t *up = lv_button_create(row);
        lv_obj_set_size(up, 68, 60);
        lv_obj_set_style_bg_color(up, lv_color_hex(0x334155), 0);
        lv_obj_add_event_cb(up, steps[s].up, LV_EVENT_CLICKED, NULL);
        lv_obj_t *ul = lv_label_create(up);
        lv_label_set_text(ul, LV_SYMBOL_PLUS);
        lv_obj_set_style_text_font(ul, &lv_font_montserrat_28, 0);
        lv_obj_center(ul);
    }

    lv_obj_t *save = lv_button_create(s_ov_autorow);
    lv_obj_set_size(save, 240, 60);
    lv_obj_set_style_bg_color(save, lv_color_hex(0x16a34a), 0);
    lv_obj_add_event_cb(save, ov_save_policy_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *sl = lv_label_create(save);
    lv_label_set_text(sl, "Save automation");
    lv_obj_set_style_text_font(sl, &lv_font_montserrat_20, 0);
    lv_obj_center(sl);

    // Close (last)
    lv_obj_t *close = lv_button_create(s_cmd_ov);
    lv_obj_set_size(close, 340, 56);
    lv_obj_set_style_bg_color(close, lv_color_hex(0x1e293b), 0);
    lv_obj_set_style_margin_top(close, 22, 0);
    lv_obj_add_event_cb(close, cmd_close_cb, LV_EVENT_CLICKED, NULL);
    lv_obj_t *cl = lv_label_create(close);
    lv_label_set_text(cl, "Close");
    lv_obj_set_style_text_font(cl, &lv_font_montserrat_20, 0);
    lv_obj_center(cl);

    s_cmd_q = xQueueCreate(8, sizeof(struct cmd_req));
    xTaskCreate(cmd_worker, "uicmd", 6144, NULL, 4, NULL);
    ESP_LOGI(TAG, "ui_controls ready");
}
