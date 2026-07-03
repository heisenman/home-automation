// Admin session + status line (see ui_admin.h). Extracted from ui_tiles.c verbatim (ADR-0020
// module-first); behavior identical. toast() -> ui_toast() (now the shared reporting channel);
// scene_btn_cb/ov_send_override/ov_save_policy_cb enqueue via the typed submitters below instead
// of building struct admin_req themselves, so ui_scenes/ui_controls need no knowledge of the queue.
#include "ui/ui_admin.h"
#include "ui/ui_http.h"     // send_json + base URL
#include <string.h>
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "mbedtls/sha256.h"
#include "cJSON.h"
#include "lvgl.h"
#include "esp_lvgl_port.h"

static const char *TAG = "ui.admin";

// ── top bar chrome owned here: gear lock/unlock button + transient status line ──
static lv_obj_t *s_admin_btn, *s_admin_lbl;
static lv_obj_t *s_toast;                        // transient status line in the top bar

// admin session: an admin JWT held ONLY in RAM (minted by /auth/login), for scene/policy edits.
// Locked = operator PANEL_TOKEN only (view + basic device on/off). Idle-auto-locks (relative esp_timer).
static char s_admin_tok[420];
static volatile bool s_admin_active;
static int64_t s_admin_last_us;                 // last admin activity (esp_timer; no wall clock needed)
#define ADMIN_IDLE_US (5 * 60 * 1000000LL)      // auto-lock after 5 min idle

// admin requests -> admin_worker (HTTP off the click stack). kind: 0=login(arg=password)
// 1=set-scene(arg=scene) 2=override(id, arg=JSON body) 3=policy(id, arg=JSON body).
struct admin_req { int kind; char id[40]; char arg[448]; };
static QueueHandle_t s_admin_q;
static lv_obj_t *s_kb_ov, *s_kb_ta, *s_kb_msg;   // password keyboard overlay

void ui_toast(const char *msg)   // transient status line in the top bar (self-locks)
{
    if (lvgl_port_lock(0)) { if (s_toast) lv_label_set_text(s_toast, msg); lvgl_port_unlock(); }
}

bool ui_admin_active(void) { return s_admin_active; }

// The control API's admin credential = SHA256("ha-api:"+passphrase) hex (server secret_store.api_token).
// The user types the memorable passphrase; we hash it here exactly like the PWA — the raw master never
// leaves the panel. out must be >= 65 bytes.
static void api_token_hex(const char *passphrase, char *out)
{
    char salted[520];
    int n = snprintf(salted, sizeof(salted), "ha-api:%s", passphrase);
    unsigned char h[32];
    mbedtls_sha256((const unsigned char *)salted, n, h, 0);   // 0 = SHA-256 (not SHA-224)
    for (int i = 0; i < 32; i++) snprintf(out + i * 2, 3, "%02x", h[i]);
    out[64] = 0;
}

static void admin_paint_ui(void)     // reflect lock state on the gear button (self-locks)
{
    if (!lvgl_port_lock(0)) return;
    if (s_admin_lbl) lv_label_set_text(s_admin_lbl, s_admin_active ? LV_SYMBOL_SETTINGS " Admin"
                                                                    : LV_SYMBOL_SETTINGS " Locked");
    if (s_admin_btn) lv_obj_set_style_bg_color(s_admin_btn,
                        lv_color_hex(s_admin_active ? 0x16a34a : 0x334155), 0);
    lvgl_port_unlock();
}

static void admin_relock(void)       // discard the RAM admin JWT -> back to operator-only
{
    s_admin_active = false;
    s_admin_tok[0] = 0;
    admin_paint_ui();
}

static void admin_worker(void *pv)
{
    struct admin_req rq;
    static char resp[640];
    for (;;) {
        if (xQueueReceive(s_admin_q, &rq, portMAX_DELAY) != pdTRUE) continue;
        char url[256];
        if (rq.kind == 0) {                                   // login: passphrase -> api token -> admin JWT
            snprintf(url, sizeof(url), "%s/auth/login", ui_http_base());
            char cred[65];
            api_token_hex(rq.arg, cred);                      // hash the typed passphrase (PWA-compatible)
            char body[128];
            snprintf(body, sizeof(body), "{\"password\":\"%s\"}", cred);
            int code = ui_http_send_json(HTTP_METHOD_POST, url, body, NULL, resp, sizeof(resp));
            if (code == 200) {
                cJSON *r = cJSON_Parse(resp);
                cJSON *t = r ? cJSON_GetObjectItem(r, "token") : NULL;
                if (cJSON_IsString(t)) {
                    snprintf(s_admin_tok, sizeof(s_admin_tok), "%s", t->valuestring);
                    s_admin_active = true;
                    s_admin_last_us = esp_timer_get_time();
                    admin_paint_ui();
                    ui_toast("admin unlocked");
                } else ui_toast("login: no token");
                if (r) cJSON_Delete(r);
            } else ui_toast(code == 401 ? "wrong password" : "login failed");
        } else {
            // all other admin actions require the RAM admin JWT
            if (!s_admin_active) { ui_toast("unlock first"); continue; }
            const char *tok = s_admin_tok;
            int code = -1;
            if (rq.kind == 1) {                               // set whole-house scene
                snprintf(url, sizeof(url), "%s/control/house/scene", ui_http_base());
                char body[460];
                snprintf(body, sizeof(body), "{\"scene\":\"%s\"}", rq.arg);
                code = ui_http_send_json(HTTP_METHOD_POST, url, body, tok, NULL, 0);
                if (code == 200) ui_toast("scene set");
            } else if (rq.kind == 2) {                        // manual override (arg = JSON body)
                snprintf(url, sizeof(url), "%s/control/%s/override", ui_http_base(), rq.id);
                code = ui_http_send_json(HTTP_METHOD_POST, url, rq.arg, tok, NULL, 0);
                if (code == 200) ui_toast("override applied");
            } else if (rq.kind == 3) {                        // automation policy edit (PUT, arg = JSON body)
                snprintf(url, sizeof(url), "%s/control/%s/policy", ui_http_base(), rq.id);
                code = ui_http_send_json(HTTP_METHOD_PUT, url, rq.arg, tok, resp, sizeof(resp));
                if (code == 200) ui_toast("automation saved");
            }
            if (code == 200) s_admin_last_us = esp_timer_get_time();
            else if (code == 401 || code == 403) { admin_relock(); ui_toast("session expired — unlock"); }
            else if (code >= 400) ui_toast("rejected by server");
            else ui_toast("request failed");
        }
    }
}

// ── typed admin submitters (enqueue only; the worker performs the HTTP) ──
void ui_admin_set_scene(const char *scene)
{
    struct admin_req rq = { .kind = 1 };
    snprintf(rq.arg, sizeof(rq.arg), "%s", scene);
    if (s_admin_q) xQueueSend(s_admin_q, &rq, 0);
}
void ui_admin_set_override(const char *id, const char *body)
{
    struct admin_req rq = { .kind = 2 };
    snprintf(rq.id, sizeof(rq.id), "%s", id);
    snprintf(rq.arg, sizeof(rq.arg), "%s", body);
    if (s_admin_q) xQueueSend(s_admin_q, &rq, 0);
}
void ui_admin_set_policy(const char *id, const char *body)
{
    struct admin_req rq = { .kind = 3 };
    snprintf(rq.id, sizeof(rq.id), "%s", id);
    snprintf(rq.arg, sizeof(rq.arg), "%s", body);
    if (s_admin_q) xQueueSend(s_admin_q, &rq, 0);
}

// idle auto-lock: drop the admin session after inactivity (relative esp_timer, no wall clock)
void ui_admin_check_idle(void)
{
    if (s_admin_active && esp_timer_get_time() - s_admin_last_us > ADMIN_IDLE_US) {
        admin_relock();
        ui_toast("admin auto-locked (idle)");
    }
}

// password keyboard: checkmark submits, X cancels.
static void kb_event_cb(lv_event_t *e)
{
    lv_event_code_t code = lv_event_get_code(e);
    if (code == LV_EVENT_READY) {
        struct admin_req rq = { .kind = 0 };
        snprintf(rq.arg, sizeof(rq.arg), "%s", lv_textarea_get_text(s_kb_ta));
        if (s_admin_q) xQueueSend(s_admin_q, &rq, 0);
        if (s_toast) lv_label_set_text(s_toast, "authenticating…");
        lv_obj_add_flag(s_kb_ov, LV_OBJ_FLAG_HIDDEN);
    } else if (code == LV_EVENT_CANCEL) {
        lv_obj_add_flag(s_kb_ov, LV_OBJ_FLAG_HIDDEN);
    }
}

// gear button: unlocked -> lock; locked -> open the password keyboard.
static void admin_btn_cb(lv_event_t *e)
{
    (void)e;
    if (s_admin_active) { admin_relock(); if (s_toast) lv_label_set_text(s_toast, "locked"); return; }
    if (s_kb_ov) {
        lv_textarea_set_text(s_kb_ta, "");
        lv_label_set_text(s_kb_msg, "enter admin password");
        lv_obj_clear_flag(s_kb_ov, LV_OBJ_FLAG_HIDDEN);
        lv_obj_move_foreground(s_kb_ov);
    }
}

void ui_admin_init(lv_obj_t *topbar)
{
    s_toast = lv_label_create(topbar);             // transient status
    lv_label_set_text(s_toast, "");
    lv_obj_set_style_text_color(s_toast, lv_color_hex(0x8fb4ff), 0);
    lv_obj_set_style_pad_right(s_toast, 10, 0);

    s_admin_btn = lv_button_create(topbar);        // lock/unlock gear
    lv_obj_set_height(s_admin_btn, 46);
    lv_obj_set_style_bg_color(s_admin_btn, lv_color_hex(0x334155), 0);
    lv_obj_add_event_cb(s_admin_btn, admin_btn_cb, LV_EVENT_CLICKED, NULL);
    s_admin_lbl = lv_label_create(s_admin_btn);
    lv_label_set_text(s_admin_lbl, LV_SYMBOL_SETTINGS " Locked");
    lv_obj_set_style_text_font(s_admin_lbl, &lv_font_montserrat_20, 0);
    lv_obj_center(s_admin_lbl);

    // password keyboard overlay (admin unlock) — top layer, hidden until the gear is tapped
    s_kb_ov = lv_obj_create(lv_layer_top());
    lv_obj_set_size(s_kb_ov, lv_pct(100), lv_pct(100));
    lv_obj_set_style_bg_color(s_kb_ov, lv_color_hex(0x0b1021), 0);
    lv_obj_set_style_bg_opa(s_kb_ov, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(s_kb_ov, 0, 0);
    lv_obj_set_style_pad_all(s_kb_ov, 20, 0);
    lv_obj_set_flex_flow(s_kb_ov, LV_FLEX_FLOW_COLUMN);
    lv_obj_add_flag(s_kb_ov, LV_OBJ_FLAG_HIDDEN);
    lv_obj_clear_flag(s_kb_ov, LV_OBJ_FLAG_SCROLLABLE);

    s_kb_msg = lv_label_create(s_kb_ov);
    lv_label_set_text(s_kb_msg, "enter admin password");
    lv_obj_set_style_text_font(s_kb_msg, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(s_kb_msg, lv_color_hex(0xffffff), 0);
    lv_obj_set_style_pad_bottom(s_kb_msg, 8, 0);

    s_kb_ta = lv_textarea_create(s_kb_ov);
    lv_obj_set_width(s_kb_ta, lv_pct(100));
    lv_obj_set_height(s_kb_ta, 72);                                // roomy entry box (was tiny vs keys)
    lv_textarea_set_one_line(s_kb_ta, true);
    lv_textarea_set_password_mode(s_kb_ta, true);
    lv_textarea_set_placeholder_text(s_kb_ta, "password");
    lv_obj_set_style_text_font(s_kb_ta, &lv_font_montserrat_28, 0);
    lv_obj_set_style_bg_color(s_kb_ta, lv_color_hex(0x1e293b), 0);
    lv_obj_set_style_text_color(s_kb_ta, lv_color_hex(0xffffff), 0);
    lv_obj_set_style_pad_all(s_kb_ta, 14, 0);
    lv_obj_set_style_margin_bottom(s_kb_ta, 12, 0);

    lv_obj_t *kb = lv_keyboard_create(s_kb_ov);
    lv_obj_set_width(kb, lv_pct(100));
    lv_obj_set_height(kb, 470);                                    // FIXED height (was flex_grow -> absurd keys)
    lv_keyboard_set_mode(kb, LV_KEYBOARD_MODE_TEXT_LOWER);         // start on letters, not symbols
    lv_keyboard_set_textarea(kb, s_kb_ta);
    // dark, high-contrast keys (the default theme rendered washed-out; keys now sanely proportioned)
    lv_obj_set_style_bg_color(kb, lv_color_hex(0x0b1021), 0);
    lv_obj_set_style_pad_all(kb, 6, 0);
    lv_obj_set_style_pad_gap(kb, 6, 0);
    lv_obj_set_style_bg_color(kb, lv_color_hex(0x243044), LV_PART_ITEMS);
    lv_obj_set_style_text_color(kb, lv_color_hex(0xffffff), LV_PART_ITEMS);
    lv_obj_set_style_text_font(kb, &lv_font_montserrat_20, LV_PART_ITEMS);
    lv_obj_set_style_radius(kb, 6, LV_PART_ITEMS);
    lv_obj_add_event_cb(kb, kb_event_cb, LV_EVENT_READY, NULL);    // checkmark -> submit
    lv_obj_add_event_cb(kb, kb_event_cb, LV_EVENT_CANCEL, NULL);   // X -> close

    s_admin_q = xQueueCreate(4, sizeof(struct admin_req));   // login + scene-set (admin)
    xTaskCreate(admin_worker, "uiadmin", 8192, NULL, 4, NULL);
    ESP_LOGI(TAG, "ui_admin ready");
}
