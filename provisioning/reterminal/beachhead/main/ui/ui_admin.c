// Admin session + status line (see ui_admin.h).
//
// D1001 displays are UNLOCKED for now — physical access is trusted (feedback-displays-unlocked): there
// is NO on-screen lock/gear/keyboard, and every control is available. The panel still authenticates to
// the control API under the hood — it holds an admin JWT in RAM that it mints automatically at boot from
// a stored api-token (secrets.h ADMIN_API_TOKEN = SHA256("ha-api:"+master)) and re-mints on 401/expiry.
// UI visibility (always on) is intentionally decoupled from that JWT. On-screen admin hardening (a real
// lock) is deferred to a later update. Owns the shared toast line every module reports through.
#include "ui/ui_admin.h"
#include "ui/ui_http.h"     // send_json + base URL
#include "secrets.h"        // ADMIN_API_TOKEN (gitignored; the one-way api-token, master stays on .210)
#include <string.h>
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include "cJSON.h"
#include "lvgl.h"
#include "esp_lvgl_port.h"

static const char *TAG = "ui.admin";

static lv_obj_t *s_toast;            // transient status line in the top bar
static char s_admin_tok[420];        // admin JWT, RAM only (minted by /auth/login)
static volatile bool s_jwt_valid;    // do we currently hold a usable JWT?

// admin requests -> admin_worker (HTTP off the click stack). kind: 1=scene 2=override 3=policy.
struct admin_req { int kind; char id[40]; char arg[448]; };
static QueueHandle_t s_admin_q;

void ui_toast(const char *msg)   // transient status line in the top bar (self-locks)
{
    if (lvgl_port_lock(0)) { if (s_toast) lv_label_set_text(s_toast, msg); lvgl_port_unlock(); }
}

// Displays are unlocked (physical access trusted): the UI always shows every control. The real auth is
// the JWT managed below — this is deliberately decoupled so the panel never prompts a person.
bool ui_admin_active(void) { return true; }

// (Re)mint the admin JWT from the stored api-token. Runs in the worker task (blocking HTTP). No user
// interaction — this is the auto-admin path. Returns true on a fresh JWT.
static bool do_login(void)
{
    char url[256];
    snprintf(url, sizeof(url), "%s/auth/login", ui_http_base());
    char body[128];
    snprintf(body, sizeof(body), "{\"password\":\"%s\"}", ADMIN_API_TOKEN);
    static char resp[640];
    int code = ui_http_send_json(HTTP_METHOD_POST, url, body, NULL, resp, sizeof(resp));
    if (code == 200) {
        cJSON *r = cJSON_Parse(resp);
        cJSON *t = r ? cJSON_GetObjectItem(r, "token") : NULL;
        bool ok = cJSON_IsString(t);
        if (ok) { snprintf(s_admin_tok, sizeof(s_admin_tok), "%s", t->valuestring); s_jwt_valid = true; }
        if (r) cJSON_Delete(r);
        if (ok) { ESP_LOGI(TAG, "admin JWT minted (auto-admin)"); return true; }
    }
    ESP_LOGW(TAG, "auto-login failed (code %d) — will retry on next action", code);
    s_jwt_valid = false;
    return false;
}

static bool ensure_admin(void) { return s_jwt_valid ? true : do_login(); }

// Perform one enqueued admin action with the held JWT. Returns the HTTP code (or -1).
static int do_action(const struct admin_req *rq)
{
    char url[256];
    if (rq->kind == 1) {                                   // set whole-house scene
        snprintf(url, sizeof(url), "%s/control/house/scene", ui_http_base());
        char body[460];
        snprintf(body, sizeof(body), "{\"scene\":\"%s\"}", rq->arg);
        return ui_http_send_json(HTTP_METHOD_POST, url, body, s_admin_tok, NULL, 0);
    } else if (rq->kind == 2) {                            // manual override (arg = JSON body)
        snprintf(url, sizeof(url), "%s/control/%s/override", ui_http_base(), rq->id);
        return ui_http_send_json(HTTP_METHOD_POST, url, rq->arg, s_admin_tok, NULL, 0);
    } else if (rq->kind == 3) {                            // automation policy edit (PUT, arg = JSON body)
        static char resp[640];
        snprintf(url, sizeof(url), "%s/control/%s/policy", ui_http_base(), rq->id);
        return ui_http_send_json(HTTP_METHOD_PUT, url, rq->arg, s_admin_tok, resp, sizeof(resp));
    } else if (rq->kind == 4) {                            // device relocate (POST, arg = JSON body)
        snprintf(url, sizeof(url), "%s/api/v1/devices/%s/relocate", ui_http_base(), rq->id);
        return ui_http_send_json(HTTP_METHOD_POST, url, rq->arg, s_admin_tok, NULL, 0);
    } else if (rq->kind == 5) {                            // device meta overlay: name/hidden/retired (PUT)
        snprintf(url, sizeof(url), "%s/api/v1/devices/%s/meta", ui_http_base(), rq->id);
        return ui_http_send_json(HTTP_METHOD_PUT, url, rq->arg, s_admin_tok, NULL, 0);
    }
    return -1;
}

static void admin_worker(void *pv)
{
    do_login();   // warm the JWT at boot (best-effort; lazily re-minted on any action)
    struct admin_req rq;
    for (;;) {
        if (xQueueReceive(s_admin_q, &rq, portMAX_DELAY) != pdTRUE) continue;
        if (!ensure_admin()) { ui_toast("auth failed"); continue; }
        int code = do_action(&rq);
        if (code == 401 || code == 403) {          // JWT expired -> re-mint once and retry
            s_jwt_valid = false;
            if (ensure_admin()) code = do_action(&rq);
        }
        const char *ok = rq.kind == 1 ? "scene set" : rq.kind == 2 ? "override applied"
                       : rq.kind == 3 ? "automation saved" : rq.kind == 4 ? "device moved" : "device updated";
        if (code == 200) ui_toast(ok);
        else if (code >= 400) ui_toast("rejected by server");
        else ui_toast("request failed");
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
void ui_admin_set_relocate(const char *id, const char *body)
{
    struct admin_req rq = { .kind = 4 };
    snprintf(rq.id, sizeof(rq.id), "%s", id);
    snprintf(rq.arg, sizeof(rq.arg), "%s", body);
    if (s_admin_q) xQueueSend(s_admin_q, &rq, 0);
}
void ui_admin_set_meta(const char *id, const char *body)
{
    struct admin_req rq = { .kind = 5 };
    snprintf(rq.id, sizeof(rq.id), "%s", id);
    snprintf(rq.arg, sizeof(rq.arg), "%s", body);
    if (s_admin_q) xQueueSend(s_admin_q, &rq, 0);
}

// No idle auto-lock while displays are unlocked — the JWT self-refreshes on 401/expiry via ensure_admin().
void ui_admin_check_idle(void) { }

void ui_admin_init(lv_obj_t *topbar)
{
    s_toast = lv_label_create(topbar);             // shared transient status line
    lv_label_set_text(s_toast, "");
    lv_obj_set_style_text_color(s_toast, lv_color_hex(0x8fb4ff), 0);
    lv_obj_set_style_pad_right(s_toast, 10, 0);

    s_admin_q = xQueueCreate(4, sizeof(struct admin_req));   // scene / override / policy
    xTaskCreate(admin_worker, "uiadmin", 8192, NULL, 4, NULL);
    ESP_LOGI(TAG, "ui_admin ready (displays unlocked; auto-admin)");
}
