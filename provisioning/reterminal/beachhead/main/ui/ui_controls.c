// Actuator controls (see ui_controls.h). shared-ui-spec Phase 2: the command overlay is now a
// THIN RENDERER of the BFF's server-authored `vm.controls` (from /api/v1/displays), matching the
// PWA. Each actuator tap rebuilds the overlay body from that device's controls[] spec — override
// presets, setpoint, ranged, indicator — plus the hysteresis policy editor (from vm.control). All
// action contracts (path/trait/action/arg_key/ranges/labels) come from the server; the panel no
// longer maps traits→widgets client-side. Power is automation-managed (override), so the old raw
// switchable ON/OFF is gone. Everything is admin-gated (shown only when unlocked). Deps: ui_admin
// (active/set_override/set_policy), ui_http (base/post_cmd).
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
#include "secrets.h"        // PANEL_TOKEN (operator JWT); "" => read-only, no control overlay

#define HAVE_TOKEN (sizeof(PANEL_TOKEN) > 1)

static const char *TAG = "ui.controls";

// --- command path (touch a control -> POST /devices/<id>/command with the operator token) ---
struct cmd_req { char id[40]; char body[192]; };
static QueueHandle_t s_cmd_q;      // command requests -> cmd_worker (HTTP POST off the LVGL stack)
struct act_ref {
    char id[40]; char name[40]; bool running;
    // hysteresis policy (from vm.control) — seeds the automation editor; not in the controls[] spec
    bool has_policy; char strategy[16]; double on_above, off_below; bool has_override;
    // the device's server-authored control spec + current actuator telemetry, duplicated so the
    // overlay can render from them after the fetch buffer is freed (freed in ui_controls_reset).
    cJSON *j_controls;   // vm.controls array
    cJSON *j_actuator;   // vm.actuator object (current values for now_key lookups)
};
#define MAX_ACTS 12
static struct act_ref s_acts[MAX_ACTS];
static int s_nacts;

static lv_obj_t *s_cmd_ov, *s_cmd_title, *s_cmd_result, *s_cmd_body;  // overlay (body rebuilt per tap)
static char s_cmd_target[40];      // device_id the overlay currently controls

// ── per-open working state: parsed from the tapped device's spec, read by the widget callbacks
// (only one overlay is open at a time, so plain statics are safe). ──
#define MAX_PRESET 4
static struct { char action[16]; int duration; bool has_dur; } s_pset[MAX_PRESET];
static int s_npset;
static char s_sp_trait[16], s_sp_action[8], s_sp_argkey[8], s_sp_unit[8];
static double s_sp_min, s_sp_max, s_sp_val;
static lv_obj_t *s_sp_lbl;
static char s_rg_trait[16], s_rg_action[8], s_rg_argkey[8];
static char s_ind_trait[16], s_ind_action[8], s_ind_argkey[8];
static double s_edit_on_above, s_edit_off_below;   // hysteresis policy editor
static lv_obj_t *s_pol_on, *s_pol_off;

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

// One generic issuer for every command-control kind: trait/action/arg_key come from the spec's
// action contract; jval is a raw JSON literal (a number for setpoint/ranged, true/false for indicator).
static void cmd_issue(const char *trait, const char *action, const char *argkey, const char *jval)
{
    if (!s_cmd_q || !s_cmd_target[0]) return;
    struct cmd_req req;
    snprintf(req.id, sizeof(req.id), "%s", s_cmd_target);
    snprintf(req.body, sizeof(req.body),
             "{\"trait\":\"%s\",\"action\":\"%s\",\"args\":{\"%s\":%s}}", trait, action, argkey, jval);
    if (xQueueSend(s_cmd_q, &req, 0) == pdTRUE && s_cmd_result) lv_label_set_text(s_cmd_result, "sending…");
}

static void cmd_close_cb(lv_event_t *e){ (void)e; lv_obj_add_flag(s_cmd_ov, LV_OBJ_FLAG_HIDDEN); }

// ── override presets → POST /control/{id}/override (admin JWT via ui_admin) ──
static void preset_cb(lv_event_t *e)
{
    int i = (int)(intptr_t)lv_event_get_user_data(e);
    if (i < 0 || i >= s_npset) return;
    char body[80];
    if (s_pset[i].has_dur)
        snprintf(body, sizeof(body), "{\"action\":\"%s\",\"duration_min\":%d}", s_pset[i].action, s_pset[i].duration);
    else
        snprintf(body, sizeof(body), "{\"action\":\"%s\"}", s_pset[i].action);
    ui_admin_set_override(s_cmd_target, body);
    if (s_cmd_result) lv_label_set_text(s_cmd_result, "applying…");
}

// ── setpoint stepper → generic command issuer ──
static void sp_update(void) { if (s_sp_lbl) { char b[24]; snprintf(b, sizeof(b), "%.0f%s", s_sp_val, s_sp_unit); lv_label_set_text(s_sp_lbl, b); } }
static void sp_dn(lv_event_t *e){ (void)e; if (s_sp_val > s_sp_min) s_sp_val -= 1; sp_update(); }
static void sp_up(lv_event_t *e){ (void)e; if (s_sp_val < s_sp_max) s_sp_val += 1; sp_update(); }
static void sp_set_cb(lv_event_t *e){ (void)e; char v[16]; snprintf(v, sizeof(v), "%.0f", s_sp_val); cmd_issue(s_sp_trait, s_sp_action, s_sp_argkey, v); }

// ── ranged option button (user_data = level value) → generic command issuer ──
static void rg_opt_cb(lv_event_t *e){ int val = (int)(intptr_t)lv_event_get_user_data(e); char v[12]; snprintf(v, sizeof(v), "%d", val); cmd_issue(s_rg_trait, s_rg_action, s_rg_argkey, v); }

// ── indicator on/off → generic command issuer ──
static void ind_on_cb(lv_event_t *e) { (void)e; cmd_issue(s_ind_trait, s_ind_action, s_ind_argkey, "true"); }
static void ind_off_cb(lv_event_t *e){ (void)e; cmd_issue(s_ind_trait, s_ind_action, s_ind_argkey, "false"); }

// ── hysteresis policy editor → PUT /control/{id}/policy (admin JWT via ui_admin) ──
static void pol_update(void)
{
    char b[24];
    if (s_pol_on)  { snprintf(b, sizeof(b), "on > %.0f",  s_edit_on_above);  lv_label_set_text(s_pol_on, b); }
    if (s_pol_off) { snprintf(b, sizeof(b), "off < %.0f", s_edit_off_below); lv_label_set_text(s_pol_off, b); }
}
static void pol_oa_dn(lv_event_t *e){ (void)e; if (s_edit_on_above > s_edit_off_below + 1) s_edit_on_above -= 1; pol_update(); }
static void pol_oa_up(lv_event_t *e){ (void)e; if (s_edit_on_above < 95) s_edit_on_above += 1; pol_update(); }
static void pol_ob_dn(lv_event_t *e){ (void)e; if (s_edit_off_below > 5) s_edit_off_below -= 1; pol_update(); }
static void pol_ob_up(lv_event_t *e){ (void)e; if (s_edit_off_below < s_edit_on_above - 1) s_edit_off_below += 1; pol_update(); }
static void pol_save_cb(lv_event_t *e)
{
    (void)e;
    char body[160];
    snprintf(body, sizeof(body),
        "{\"enabled\":true,\"control\":{\"strategy\":\"hysteresis\",\"on_above\":%.0f,\"off_below\":%.0f}}",
        s_edit_on_above, s_edit_off_below);
    ui_admin_set_policy(s_cmd_target, body);
    if (s_cmd_result) lv_label_set_text(s_cmd_result, "saving…");
}

// ── small layout helpers (build into the dynamic overlay body) ──
static lv_obj_t *mk_row(lv_obj_t *parent)   // transparent horizontal row
{
    lv_obj_t *r = lv_obj_create(parent);
    lv_obj_set_width(r, LV_SIZE_CONTENT);
    lv_obj_set_height(r, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(r, 0, 0);
    lv_obj_set_style_border_width(r, 0, 0);
    lv_obj_set_style_pad_all(r, 0, 0);
    lv_obj_set_style_pad_column(r, 10, 0);
    lv_obj_set_flex_flow(r, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(r, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(r, LV_OBJ_FLAG_SCROLLABLE);
    return r;
}
static lv_obj_t *mk_btn(lv_obj_t *parent, const char *txt, uint32_t col, int w, int h, lv_event_cb_t cb, void *ud)
{
    lv_obj_t *b = lv_button_create(parent);
    lv_obj_set_size(b, w, h);
    lv_obj_set_style_bg_color(b, lv_color_hex(col), 0);
    lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, ud);
    lv_obj_t *l = lv_label_create(b);
    lv_label_set_text(l, txt);
    lv_obj_set_style_text_font(l, &lv_font_montserrat_20, 0);
    lv_obj_center(l);
    return b;
}
static lv_obj_t *mk_caption(lv_obj_t *parent, const char *txt)
{
    lv_obj_t *l = lv_label_create(parent);
    lv_label_set_text(l, txt);
    lv_obj_set_style_text_color(l, lv_color_hex(0xfbbf24), 0);
    lv_obj_set_style_pad_top(l, 10, 0);
    return l;
}

static void get_action(cJSON *c, char *trait, char *action, char *argkey)
{
    cJSON *a  = cJSON_GetObjectItem(c, "action");
    cJSON *t  = a ? cJSON_GetObjectItem(a, "trait")   : NULL;
    cJSON *ac = a ? cJSON_GetObjectItem(a, "action")  : NULL;
    cJSON *ak = a ? cJSON_GetObjectItem(a, "arg_key") : NULL;
    snprintf(trait,  16, "%s", cJSON_IsString(t)  ? t->valuestring  : "");
    snprintf(action,  8, "%s", cJSON_IsString(ac) ? ac->valuestring : "set");
    snprintf(argkey,  8, "%s", cJSON_IsString(ak) ? ak->valuestring : "");
}
static double act_now(cJSON *actuator, const char *now_key, bool *has)
{
    *has = false;
    if (!actuator || !now_key || !now_key[0]) return 0;
    cJSON *v = cJSON_GetObjectItem(actuator, now_key);
    if (cJSON_IsNumber(v)) { *has = true; return v->valuedouble; }
    if (cJSON_IsBool(v))   { *has = true; return cJSON_IsTrue(v) ? 1 : 0; }
    return 0;
}

static bool s_acts_override_active(void);   // fwd: override "clear" preset filter (defined below)

// Build one control widget from its spec descriptor into the overlay body.
static void build_control(cJSON *c, cJSON *actuator)
{
    cJSON *jk = cJSON_GetObjectItem(c, "kind");
    const char *kind = cJSON_IsString(jk) ? jk->valuestring : "";
    cJSON *jl = cJSON_GetObjectItem(c, "label");
    const char *label = cJSON_IsString(jl) ? jl->valuestring : kind;

    if (strcmp(kind, "override") == 0) {
        mk_caption(s_cmd_body, label[0] ? label : "Power");
        lv_obj_t *row = mk_row(s_cmd_body);
        cJSON *presets = cJSON_GetObjectItem(c, "presets");
        s_npset = 0;
        cJSON *p;
        cJSON_ArrayForEach(p, presets) {
            if (s_npset >= MAX_PRESET) break;
            cJSON *pa = cJSON_GetObjectItem(p, "action");
            if (!cJSON_IsString(pa)) continue;
            cJSON *pw = cJSON_GetObjectItem(p, "when");
            // "clear"/Resume shows only while an override is active (matches the PWA + spec `when`)
            if (cJSON_IsString(pw) && strcmp(pw->valuestring, "override_active") == 0 && !s_acts_override_active())
                continue;
            cJSON *pd = cJSON_GetObjectItem(p, "duration_min");
            cJSON *pl = cJSON_GetObjectItem(p, "label");
            int i = s_npset++;
            snprintf(s_pset[i].action, sizeof(s_pset[i].action), "%s", pa->valuestring);
            s_pset[i].has_dur = cJSON_IsNumber(pd);
            s_pset[i].duration = s_pset[i].has_dur ? (int)pd->valuedouble : 0;
            uint32_t col = strcmp(pa->valuestring, "clear") == 0 ? 0x1e293b : 0x2563eb;
            mk_btn(row, cJSON_IsString(pl) ? pl->valuestring : pa->valuestring, col, 180, 60, preset_cb, (void *)(intptr_t)i);
        }
    } else if (strcmp(kind, "setpoint") == 0) {
        get_action(c, s_sp_trait, s_sp_action, s_sp_argkey);
        cJSON *ju = cJSON_GetObjectItem(c, "unit");
        cJSON *jmin = cJSON_GetObjectItem(c, "min");
        cJSON *jmax = cJSON_GetObjectItem(c, "max");
        cJSON *jnk = cJSON_GetObjectItem(c, "now_key");
        cJSON *jsafe = cJSON_GetObjectItem(c, "safe_value");
        snprintf(s_sp_unit, sizeof(s_sp_unit), "%s", cJSON_IsString(ju) ? ju->valuestring : "");
        s_sp_min = cJSON_IsNumber(jmin) ? jmin->valuedouble : 0;
        s_sp_max = cJSON_IsNumber(jmax) ? jmax->valuedouble : 100;
        bool hn; double now = act_now(actuator, cJSON_IsString(jnk) ? jnk->valuestring : NULL, &hn);
        s_sp_val = hn ? now : (cJSON_IsNumber(jsafe) ? jsafe->valuedouble : s_sp_min);
        mk_caption(s_cmd_body, label);
        lv_obj_t *row = mk_row(s_cmd_body);
        mk_btn(row, LV_SYMBOL_MINUS, 0x334155, 68, 60, sp_dn, NULL);
        s_sp_lbl = lv_label_create(row);
        lv_obj_set_width(s_sp_lbl, 130);
        lv_obj_set_style_text_font(s_sp_lbl, &lv_font_montserrat_28, 0);
        lv_obj_set_style_text_color(s_sp_lbl, lv_color_hex(0xffffff), 0);
        lv_obj_set_style_text_align(s_sp_lbl, LV_TEXT_ALIGN_CENTER, 0);
        sp_update();
        mk_btn(row, LV_SYMBOL_PLUS, 0x334155, 68, 60, sp_up, NULL);
        mk_btn(row, "Set", 0x16a34a, 120, 60, sp_set_cb, NULL);
    } else if (strcmp(kind, "ranged") == 0) {
        get_action(c, s_rg_trait, s_rg_action, s_rg_argkey);
        cJSON *jnk = cJSON_GetObjectItem(c, "now_key");
        bool hn; double now = act_now(actuator, cJSON_IsString(jnk) ? jnk->valuestring : NULL, &hn);
        mk_caption(s_cmd_body, label);
        lv_obj_t *row = mk_row(s_cmd_body);
        cJSON *opts = cJSON_GetObjectItem(c, "options");
        cJSON *o;
        cJSON_ArrayForEach(o, opts) {
            cJSON *ov = cJSON_GetObjectItem(o, "value");
            cJSON *ol = cJSON_GetObjectItem(o, "label");
            if (!cJSON_IsNumber(ov)) continue;
            int val = (int)ov->valuedouble;
            uint32_t col = (hn && (int)now == val) ? 0x2563eb : 0x334155;   // highlight current
            mk_btn(row, cJSON_IsString(ol) ? ol->valuestring : "?", col, 120, 60, rg_opt_cb, (void *)(intptr_t)val);
        }
    } else if (strcmp(kind, "indicator") == 0) {
        get_action(c, s_ind_trait, s_ind_action, s_ind_argkey);
        cJSON *jnk = cJSON_GetObjectItem(c, "now_key");
        bool hn; double now = act_now(actuator, cJSON_IsString(jnk) ? jnk->valuestring : NULL, &hn);
        char cap[40]; snprintf(cap, sizeof(cap), "%s%s", label, hn ? (now ? "  (on)" : "  (off)") : "");
        mk_caption(s_cmd_body, cap);
        lv_obj_t *row = mk_row(s_cmd_body);
        mk_btn(row, "On",  0x16a34a, 140, 60, ind_on_cb,  NULL);
        mk_btn(row, "Off", 0x334155, 140, 60, ind_off_cb, NULL);
    }
}

// helper for the override "clear" filter — is the currently-targeted actuator overridden?
static bool s_acts_override_active(void)
{
    for (int i = 0; i < s_nacts; i++)
        if (strcmp(s_acts[i].id, s_cmd_target) == 0) return s_acts[i].has_override;
    return false;
}

static void act_clicked_cb(lv_event_t *e)
{
    int idx = (int)(intptr_t)lv_event_get_user_data(e);
    if (idx < 0 || idx >= s_nacts || !s_cmd_ov) return;
    struct act_ref *ar = &s_acts[idx];
    snprintf(s_cmd_target, sizeof(s_cmd_target), "%s", ar->id);
    lv_label_set_text(s_cmd_title, ar->name);
    if (s_cmd_result) lv_label_set_text(s_cmd_result, ar->running ? "currently ON" : "currently OFF");

    lv_obj_clean(s_cmd_body);   // rebuild the overlay body from this device's spec
    s_sp_lbl = s_pol_on = s_pol_off = NULL;

    if (!ui_admin_active()) {
        lv_obj_t *l = lv_label_create(s_cmd_body);
        lv_label_set_text(l, "Unlock (gear) to control this device.");
        lv_obj_set_style_text_color(l, lv_color_hex(0x94a3b8), 0);
        lv_obj_set_style_pad_top(l, 10, 0);
    } else {
        cJSON *c;   // server-authored controls (override / setpoint / ranged / indicator)
        cJSON_ArrayForEach(c, ar->j_controls) build_control(c, ar->j_actuator);

        // hysteresis automation editor — from vm.control (not in the controls[] spec), like the PWA
        if (ar->has_policy) {
            mk_caption(s_cmd_body, "Automation (hysteresis)");
            struct { lv_obj_t **lbl; lv_event_cb_t dn, up; } st[] = {
                {&s_pol_on,  pol_oa_dn, pol_oa_up}, {&s_pol_off, pol_ob_dn, pol_ob_up} };
            s_edit_on_above = ar->on_above; s_edit_off_below = ar->off_below;
            for (unsigned k = 0; k < 2; k++) {
                lv_obj_t *row = mk_row(s_cmd_body);
                mk_btn(row, LV_SYMBOL_MINUS, 0x334155, 68, 56, st[k].dn, NULL);
                lv_obj_t *v = lv_label_create(row);
                lv_obj_set_width(v, 150);
                lv_obj_set_style_text_font(v, &lv_font_montserrat_28, 0);
                lv_obj_set_style_text_color(v, lv_color_hex(0xffffff), 0);
                lv_obj_set_style_text_align(v, LV_TEXT_ALIGN_CENTER, 0);
                *st[k].lbl = v;
                mk_btn(row, LV_SYMBOL_PLUS, 0x334155, 68, 56, st[k].up, NULL);
            }
            pol_update();
            lv_obj_t *saverow = mk_row(s_cmd_body);
            mk_btn(saverow, "Save automation", 0x16a34a, 260, 56, pol_save_cb, NULL);
        }
    }

    // Close (always last)
    lv_obj_t *closerow = mk_row(s_cmd_body);
    mk_btn(closerow, "Close", 0x1e293b, 340, 56, cmd_close_cb, NULL);

    lv_obj_clear_flag(s_cmd_ov, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(s_cmd_ov);
}

void ui_controls_reset(void)
{
    for (int i = 0; i < s_nacts; i++) {   // free the per-device spec dups before rebuilding the registry
        if (s_acts[i].j_controls) { cJSON_Delete(s_acts[i].j_controls); s_acts[i].j_controls = NULL; }
        if (s_acts[i].j_actuator) { cJSON_Delete(s_acts[i].j_actuator); s_acts[i].j_actuator = NULL; }
    }
    s_nacts = 0;
}

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
        ar->has_override = cJSON_IsObject(cJSON_GetObjectItem(d, "override"));
        // hysteresis policy (seeds the automation editor) — from vm.control
        ar->has_policy = false; ar->on_above = ar->off_below = 0; ar->strategy[0] = 0;
        cJSON *ctrl = cJSON_GetObjectItem(d, "control");
        if (cJSON_IsObject(ctrl)) {
            cJSON *oa = cJSON_GetObjectItem(ctrl, "on_above");
            cJSON *ob = cJSON_GetObjectItem(ctrl, "off_below");
            cJSON *stt = cJSON_GetObjectItem(ctrl, "strategy");
            if (cJSON_IsString(stt)) snprintf(ar->strategy, sizeof(ar->strategy), "%s", stt->valuestring);
            if (cJSON_IsNumber(oa) && cJSON_IsNumber(ob)) {   // hysteresis thresholds -> editable
                ar->on_above = oa->valuedouble; ar->off_below = ob->valuedouble; ar->has_policy = true;
            }
        }
        // duplicate the server-authored spec + telemetry for the overlay (fetch buffer is freed after render)
        cJSON *cs = cJSON_GetObjectItem(d, "controls");
        ar->j_controls = cJSON_IsArray(cs) ? cJSON_Duplicate(cs, 1) : NULL;
        ar->j_actuator = cJSON_IsObject(act) ? cJSON_Duplicate(act, 1) : NULL;
        lv_obj_add_flag(card, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(card, act_clicked_cb, LV_EVENT_CLICKED, (void *)(intptr_t)idx);
    }
}

void ui_controls_init(void)
{
    if (!HAVE_TOKEN) return;   // read-only build: no command overlay, no worker

    // actuator command overlay (top layer, hidden until an actuator card is tapped). Scrollable
    // top-aligned column: title + result + a dynamic body (rebuilt per tap from the device's spec).
    s_cmd_ov = lv_obj_create(lv_layer_top());
    lv_obj_set_size(s_cmd_ov, lv_pct(100), lv_pct(100));
    lv_obj_set_style_bg_color(s_cmd_ov, lv_color_hex(0x0b1021), 0);
    lv_obj_set_style_bg_opa(s_cmd_ov, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(s_cmd_ov, 0, 0);
    lv_obj_set_style_pad_all(s_cmd_ov, 30, 0);
    lv_obj_set_flex_flow(s_cmd_ov, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(s_cmd_ov, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_row(s_cmd_ov, 6, 0);
    lv_obj_set_scroll_dir(s_cmd_ov, LV_DIR_VER);
    lv_obj_set_scrollbar_mode(s_cmd_ov, LV_SCROLLBAR_MODE_AUTO);
    lv_obj_add_flag(s_cmd_ov, LV_OBJ_FLAG_HIDDEN);

    s_cmd_title = lv_label_create(s_cmd_ov);
    lv_obj_set_style_text_font(s_cmd_title, &lv_font_montserrat_28, 0);
    lv_obj_set_style_text_color(s_cmd_title, lv_color_hex(0xffffff), 0);
    s_cmd_result = lv_label_create(s_cmd_ov);
    lv_obj_set_style_text_color(s_cmd_result, lv_color_hex(0x8fb4ff), 0);
    lv_obj_set_style_pad_bottom(s_cmd_result, 12, 0);

    s_cmd_body = lv_obj_create(s_cmd_ov);    // dynamic content (cleared + rebuilt on each tap)
    lv_obj_set_width(s_cmd_body, LV_SIZE_CONTENT);
    lv_obj_set_height(s_cmd_body, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(s_cmd_body, 0, 0);
    lv_obj_set_style_border_width(s_cmd_body, 0, 0);
    lv_obj_set_style_pad_all(s_cmd_body, 0, 0);
    lv_obj_set_style_pad_row(s_cmd_body, 8, 0);
    lv_obj_set_flex_flow(s_cmd_body, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(s_cmd_body, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(s_cmd_body, LV_OBJ_FLAG_SCROLLABLE);

    s_cmd_q = xQueueCreate(8, sizeof(struct cmd_req));
    xTaskCreate(cmd_worker, "uicmd", 6144, NULL, 4, NULL);
    ESP_LOGI(TAG, "ui_controls ready");
}
