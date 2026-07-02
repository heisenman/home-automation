// Server-backed LVGL tile renderer (ADR-0019 Phase 2). See ui_tiles.h.
#include "ui_tiles.h"
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_timer.h"
#include "mbedtls/sha256.h"
#include "cJSON.h"
#include "lvgl.h"
#include "esp_lvgl_port.h"
#include "secrets.h"   // PANEL_TOKEN (operator JWT); "" => read-only, no control buttons

#define HAVE_TOKEN (sizeof(PANEL_TOKEN) > 1)

static const char *TAG = "ui";
#define REFRESH_MS 10000

static char s_url[192];       // /api/v1/sensors
static char s_disp_url[192];  // /api/v1/displays (controllable devices)
static lv_obj_t *s_header, *s_grid;

// server-authored graph spec for a metric (from /api/v1/sensors `graphs`, ADR-0019 shared UI spec)
#define MAX_GRAPHS 8
struct gspec { char key[16]; char label[18]; uint32_t color; int prec; };

// Per-card registry so MQTT state updates can patch a card's headline in place.
// Written by render() and read by ui_tiles_on_state() — both hold the LVGL lock,
// which serializes them (render rebuilds the whole registry each fetch).
struct card_ref {
    char id[40];
    const char *hkey, *hlabel, *hunit;   // headline metric (point into the static FMT table)
    int hprec;
    lv_obj_t *hval;                       // the headline label widget
    char name[40];                        // display name (for the detail overlay)
    char room[28];
    char *detail;                         // heap: all metrics, one per line (freed on re-render)
    struct gspec graphs[MAX_GRAPHS];      // graphable metrics for this sensor (server spec)
    int ngraph;
};
#define MAX_CARDS 48
static struct card_ref s_cards[MAX_CARDS];
static int s_ncards;
static bool s_started;
static QueueHandle_t s_state_q;    // MQTT state payloads (char*) -> state_task (LVGL off the mqtt stack)

// ── inline expand-below panels + 72h charts (ADR-0019 Phase A; mirrors the PWA ExpandedSensor) ──
// Tapping a sensor tile appends a full-width panel under the grid (multiple stack; the root scrolls).
// Each panel charts its graphable metrics over 72h from GET /devices/{id}/readings?hours=72.
static lv_obj_t *s_expbox;         // vertical container BELOW the grid holding expansion panels
#define GRAPH_HOURS 72
#define GRAPH_POINTS 200
struct expand_ref {
    char id[40];
    char name[40];
    bool active;
    uint32_t epoch;                // bumped on close/reuse; the fetch worker compares to avoid stale writes
    lv_obj_t *panel;
    lv_obj_t *chart[MAX_GRAPHS];
    lv_chart_series_t *ser[MAX_GRAPHS];
    lv_obj_t *note[MAX_GRAPHS];    // per-chart "loading…/no data" label
    struct gspec g[MAX_GRAPHS];
    int ngraph;
};
#define MAX_EXPAND 6
static struct expand_ref s_exp[MAX_EXPAND];
static QueueHandle_t s_chart_q;    // int slot idx -> chart_worker (readings HTTP off the click stack)

// ── top bar: scene selector (Home/Away/Sleep) + admin lock/unlock (ADR-0019 Phase B) ──
static char s_house_url[256];      // /api/v1/house
static lv_obj_t *s_topbar, *s_scenebox;
#define MAX_SCENES 4
static lv_obj_t *s_scene_btn[MAX_SCENES];
static char s_scene_name[MAX_SCENES][16];
static int s_nscenes;
static char s_scene_active[16];    // currently-active scene (from /api/v1/house)
static lv_obj_t *s_admin_btn, *s_admin_lbl;

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
static lv_obj_t *s_toast;                        // transient status line in the top bar
static lv_obj_t *s_batt_lbl;                     // battery indicator in the top bar

// --- command path (touch an actuator -> POST /devices/<id>/command with the operator token) ---
static char s_base[192];           // BFF base URL (http://host:port), derived from s_url
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

// metric key -> friendly label + unit + decimal places. Order = display priority.
struct mfmt { const char *key, *label, *unit; int prec; };
static const struct mfmt FMT[] = {
    {"temperature_c", "Temp",  "C",     1},
    {"pm25_ugm3",     "PM2.5", "ug",    0},
    {"co2_ppm",       "CO2",   "ppm",   0},
    {"radon_bqm3",    "Radon", "Bq",    0},
    {"humidity_pct",  "Hum",   "%",     0},
    {"dewpoint_c",    "Dew",   "C",     1},
    {"pressure_hpa",  "Press", "hPa",   0},
    {"aqi",           "AQI",   "",      0},
    {"battery_pct",   "Batt",  "%",     0},
};
#define NFMT (sizeof(FMT)/sizeof(FMT[0]))

// ---- HTTP GET into a PSRAM buffer (caller frees) ----
static char *http_get(const char *url, int *out_len)
{
    esp_http_client_config_t cfg = { .url = url, .timeout_ms = 8000 };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) return NULL;
    char *buf = NULL; int total = 0;
    if (esp_http_client_open(c, 0) == ESP_OK) {
        int cl = esp_http_client_fetch_headers(c);
        int cap = (cl > 0) ? cl + 1 : 32768;
        buf = heap_caps_malloc(cap, MALLOC_CAP_SPIRAM);
        if (buf) {
            int r;
            while (total < cap - 1 &&
                   (r = esp_http_client_read(c, buf + total, cap - 1 - total)) > 0)
                total += r;
            buf[total] = 0;
        }
    }
    esp_http_client_close(c);
    esp_http_client_cleanup(c);
    *out_len = total;
    return buf;
}

static double metric_of(cJSON *metrics, const char *key, bool *present)
{
    cJSON *v = cJSON_GetObjectItem(metrics, key);
    *present = cJSON_IsNumber(v);
    return *present ? v->valuedouble : 0.0;
}

// "#rrggbb" -> 0xrrggbb (server graph color). Falls back to a neutral slate on any parse miss.
static uint32_t parse_hex_color(const char *s)
{
    if (s && s[0] == '#' && strlen(s) >= 7) {
        uint32_t v = (uint32_t)strtoul(s + 1, NULL, 16);
        return v & 0xFFFFFF;
    }
    return 0x8fb4ff;
}

struct chart_req { int slot; uint32_t epoch; };   // -> chart_worker (which expansion to fill)

// ---- inline expand-below panels + 72h charts (mirrors PWA ExpandedSensor) ----
// Close an expansion: delete its panel, bump epoch (so an in-flight fetch discards its
// result), mark the slot free. Caller MUST hold the LVGL lock.
static void expand_free(int slot)
{
    struct expand_ref *x = &s_exp[slot];
    if (!x->active) return;
    x->active = false;
    x->epoch++;
    if (x->panel) lv_obj_del(x->panel);   // deletes children (charts/notes) too
    x->panel = NULL;
    for (int i = 0; i < MAX_GRAPHS; i++) { x->chart[i] = NULL; x->ser[i] = NULL; x->note[i] = NULL; }
    x->ngraph = 0;
    x->id[0] = 0;
}

static void expand_close_cb(lv_event_t *e)
{
    int slot = (int)(intptr_t)lv_event_get_user_data(e);
    if (slot >= 0 && slot < MAX_EXPAND) expand_free(slot);
}

// Build the expansion panel for card `idx` and enqueue its 72h chart fetch. Runs in the
// LVGL/click context (object creation is fine here; the HTTP fetch is deferred to a worker).
static void expand_open(int idx)
{
    struct card_ref *c = &s_cards[idx];
    // toggle: if this device is already expanded, close it instead
    for (int i = 0; i < MAX_EXPAND; i++)
        if (s_exp[i].active && strcmp(s_exp[i].id, c->id) == 0) { expand_free(i); return; }
    // find a free slot
    int slot = -1;
    for (int i = 0; i < MAX_EXPAND; i++) if (!s_exp[i].active) { slot = i; break; }
    if (slot < 0) { expand_free(0); slot = 0; }     // all full: recycle the oldest (slot 0)
    struct expand_ref *x = &s_exp[slot];
    memset(x->chart, 0, sizeof(x->chart));
    snprintf(x->id, sizeof(x->id), "%s", c->id);
    snprintf(x->name, sizeof(x->name), "%s", c->name[0] ? c->name : c->id);
    x->ngraph = c->ngraph;
    for (int i = 0; i < c->ngraph; i++) x->g[i] = c->graphs[i];
    x->active = true;

    lv_obj_t *panel = lv_obj_create(s_expbox);
    x->panel = panel;
    lv_obj_set_width(panel, lv_pct(100));
    lv_obj_set_height(panel, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_color(panel, lv_color_hex(0x111834), 0);
    lv_obj_set_style_border_width(panel, 0, 0);
    lv_obj_set_style_radius(panel, 12, 0);
    lv_obj_set_style_pad_all(panel, 12, 0);
    lv_obj_set_flex_flow(panel, LV_FLEX_FLOW_COLUMN);
    lv_obj_clear_flag(panel, LV_OBJ_FLAG_SCROLLABLE);

    // header row: title (name · room)  +  Close (✕)
    lv_obj_t *hdr = lv_obj_create(panel);
    lv_obj_set_width(hdr, lv_pct(100));
    lv_obj_set_height(hdr, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(hdr, 0, 0);
    lv_obj_set_style_border_width(hdr, 0, 0);
    lv_obj_set_style_pad_all(hdr, 0, 0);
    lv_obj_set_flex_flow(hdr, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(hdr, LV_FLEX_ALIGN_SPACE_BETWEEN, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(hdr, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *ttl = lv_label_create(hdr);
    lv_label_set_text_fmt(ttl, "%s  -  %s", x->name, c->room);
    lv_obj_set_style_text_font(ttl, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(ttl, lv_color_hex(0xffffff), 0);

    lv_obj_t *cls = lv_button_create(hdr);
    lv_obj_set_size(cls, 52, 40);
    lv_obj_set_style_bg_color(cls, lv_color_hex(0x334155), 0);
    lv_obj_add_event_cb(cls, expand_close_cb, LV_EVENT_CLICKED, (void *)(intptr_t)slot);
    lv_obj_t *clsl = lv_label_create(cls);
    lv_label_set_text(clsl, LV_SYMBOL_CLOSE);
    lv_obj_center(clsl);

    // present-state line (all current metrics)
    if (c->detail && c->detail[0]) {
        lv_obj_t *st = lv_label_create(panel);
        lv_label_set_text(st, c->detail);
        lv_label_set_long_mode(st, LV_LABEL_LONG_WRAP);
        lv_obj_set_width(st, lv_pct(100));
        lv_obj_set_style_text_color(st, lv_color_hex(0xcbd5e1), 0);
        lv_obj_set_style_pad_top(st, 6, 0);
    }

    if (x->ngraph == 0) {
        lv_obj_t *none = lv_label_create(panel);
        lv_label_set_text(none, "no graphable metrics");
        lv_obj_set_style_text_color(none, lv_color_hex(0x64748b), 0);
        return;
    }

    // one chart per graphable metric (empty; the worker fills them after fetching)
    for (int i = 0; i < x->ngraph; i++) {
        lv_obj_t *lbl = lv_label_create(panel);
        lv_label_set_text(lbl, x->g[i].label);
        lv_obj_set_style_text_color(lbl, lv_color_hex(x->g[i].color), 0);
        lv_obj_set_style_pad_top(lbl, 10, 0);

        lv_obj_t *ch = lv_chart_create(panel);
        lv_obj_set_width(ch, lv_pct(100));
        lv_obj_set_height(ch, 150);
        lv_obj_set_style_bg_color(ch, lv_color_hex(0x0b1021), 0);
        lv_obj_set_style_border_width(ch, 0, 0);
        lv_obj_set_style_pad_all(ch, 4, 0);
        lv_chart_set_type(ch, LV_CHART_TYPE_LINE);
        lv_chart_set_update_mode(ch, LV_CHART_UPDATE_MODE_SHIFT);
        lv_chart_set_div_line_count(ch, 3, 0);
        lv_chart_set_point_count(ch, GRAPH_POINTS);
        lv_obj_set_style_width(ch, 0, LV_PART_INDICATOR);     // no per-point dots (dense series)
        lv_obj_set_style_height(ch, 0, LV_PART_INDICATOR);
        lv_chart_series_t *ser = lv_chart_add_series(ch, lv_color_hex(x->g[i].color),
                                                     LV_CHART_AXIS_PRIMARY_Y);
        x->chart[i] = ch;
        x->ser[i] = ser;

        lv_obj_t *nt = lv_label_create(panel);
        lv_label_set_text(nt, "loading…");
        lv_obj_set_style_text_color(nt, lv_color_hex(0x64748b), 0);
        x->note[i] = nt;
    }

    if (s_chart_q) {
        struct chart_req rq = { .slot = slot, .epoch = x->epoch };
        xQueueSend(s_chart_q, &rq, 0);
    }
}

static void card_clicked_cb(lv_event_t *e)
{
    int idx = (int)(intptr_t)lv_event_get_user_data(e);
    if (idx < 0 || idx >= s_ncards) return;
    ESP_LOGI(TAG, "tap -> card %d (%s)", idx, s_cards[idx].id);
    expand_open(idx);
}

static void card_for(cJSON *e)
{
    cJSON *metrics = cJSON_GetObjectItem(e, "metrics");
    if (!cJSON_IsObject(metrics)) return;

    const cJSON *jname = cJSON_GetObjectItem(e, "name");
    const cJSON *jroom = cJSON_GetObjectItem(e, "room");
    const cJSON *jage  = cJSON_GetObjectItem(e, "age_s");
    const cJSON *jid   = cJSON_GetObjectItem(e, "device_id");
    const char *name = cJSON_IsString(jname) ? jname->valuestring
                     : (cJSON_IsString(jid) ? jid->valuestring : "sensor");
    const char *room = cJSON_IsString(jroom) ? jroom->valuestring : "";
    int age = cJSON_IsNumber(jage) ? (int)jage->valuedouble : -1;
    bool stale = (age < 0 || age > 600);   // >10 min = stale

    lv_obj_t *card = lv_obj_create(s_grid);
    lv_obj_set_size(card, 236, 158);
    lv_obj_set_style_bg_color(card, lv_color_hex(stale ? 0x161a24 : 0x16204a), 0);
    lv_obj_set_style_border_width(card, 0, 0);
    lv_obj_set_style_radius(card, 12, 0);
    lv_obj_set_style_pad_all(card, 10, 0);
    lv_obj_set_flex_flow(card, LV_FLEX_FLOW_COLUMN);
    lv_obj_clear_flag(card, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *t = lv_label_create(card);
    lv_label_set_text(t, name);
    lv_obj_set_style_text_font(t, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(t, lv_color_hex(stale ? 0x64748b : 0xffffff), 0);
    lv_label_set_long_mode(t, LV_LABEL_LONG_DOT);
    lv_obj_set_width(t, lv_pct(100));

    lv_obj_t *r = lv_label_create(card);
    if (age >= 0) lv_label_set_text_fmt(r, "%s  -  %ds", room, age);
    else          lv_label_set_text(r, room);
    lv_obj_set_style_text_color(r, lv_color_hex(0x8fb4ff), 0);

    // headline = highest-priority present metric, shown big
    char big[48] = ""; int headline = -1;
    for (unsigned i = 0; i < NFMT && headline < 0; i++) {
        if (strcmp(FMT[i].key, "battery_pct") == 0) continue;
        bool p; double v = metric_of(metrics, FMT[i].key, &p);
        if (p) { headline = i; snprintf(big, sizeof(big), "%.*f %s", FMT[i].prec, v, FMT[i].unit); }
    }
    if (headline >= 0) {
        lv_obj_t *h = lv_label_create(card);
        lv_label_set_text_fmt(h, "%s %s", FMT[headline].label, big);
        lv_obj_set_style_text_font(h, &lv_font_montserrat_28, 0);
        lv_obj_set_style_text_color(h, lv_color_hex(stale ? 0x94a3b8 : 0xffffff), 0);
        lv_obj_set_style_pad_top(h, 4, 0);
        // register for live MQTT patching + tap-to-detail
        if (s_ncards < MAX_CARDS && cJSON_IsString(jid)) {
            int idx = s_ncards;
            struct card_ref *cr = &s_cards[s_ncards++];
            snprintf(cr->id, sizeof(cr->id), "%s", jid->valuestring);
            cr->hkey = FMT[headline].key; cr->hlabel = FMT[headline].label;
            cr->hunit = FMT[headline].unit; cr->hprec = FMT[headline].prec;
            cr->hval = h;
            snprintf(cr->name, sizeof(cr->name), "%s", name);
            snprintf(cr->room, sizeof(cr->room), "%s", room);
            char det[400]; size_t o = 0;                 // full detail: every known metric, one per line
            for (unsigned i = 0; i < NFMT; i++) {
                bool pp; double vv = metric_of(metrics, FMT[i].key, &pp);
                if (!pp) continue;
                o += snprintf(det + o, sizeof(det) - o, "%s%s: %.*f %s",
                              o ? "\n" : "", FMT[i].label, FMT[i].prec, vv, FMT[i].unit);
                if (o >= sizeof(det) - 24) break;
            }
            cr->detail = strdup(det);
            // server-authored graph spec (ADR-0019): which metrics to chart + label/color/precision
            cr->ngraph = 0;
            cJSON *graphs = cJSON_GetObjectItem(e, "graphs");
            if (cJSON_IsArray(graphs)) {
                cJSON *g;
                cJSON_ArrayForEach(g, graphs) {
                    if (cr->ngraph >= MAX_GRAPHS) break;
                    const cJSON *gk = cJSON_GetObjectItem(g, "key");
                    const cJSON *gl = cJSON_GetObjectItem(g, "label");
                    const cJSON *gc = cJSON_GetObjectItem(g, "color");
                    const cJSON *gp = cJSON_GetObjectItem(g, "precision");
                    if (!cJSON_IsString(gk)) continue;
                    struct gspec *gs = &cr->graphs[cr->ngraph++];
                    snprintf(gs->key, sizeof(gs->key), "%s", gk->valuestring);
                    snprintf(gs->label, sizeof(gs->label), "%s",
                             cJSON_IsString(gl) ? gl->valuestring : gk->valuestring);
                    gs->color = parse_hex_color(cJSON_IsString(gc) ? gc->valuestring : NULL);
                    gs->prec = cJSON_IsNumber(gp) ? (int)gp->valuedouble : 0;
                }
            }
            lv_obj_add_flag(card, LV_OBJ_FLAG_CLICKABLE);
            lv_obj_add_event_cb(card, card_clicked_cb, LV_EVENT_CLICKED, (void *)(intptr_t)idx);
        }
    }

    // remaining present metrics as a compact multiline
    char rest[192] = ""; size_t off = 0;
    for (unsigned i = 0; i < NFMT; i++) {
        if ((int)i == headline) continue;
        bool p; double v = metric_of(metrics, FMT[i].key, &p);
        if (!p) continue;
        off += snprintf(rest + off, sizeof(rest) - off, "%s%s %.*f%s",
                        off ? "   " : "", FMT[i].label, FMT[i].prec, v, FMT[i].unit);
        if (off >= sizeof(rest) - 24) break;
    }
    if (rest[0]) {
        lv_obj_t *s = lv_label_create(card);
        lv_label_set_text(s, rest);
        lv_label_set_long_mode(s, LV_LABEL_LONG_WRAP);
        lv_obj_set_width(s, lv_pct(100));
        lv_obj_set_style_text_color(s, lv_color_hex(0x94a3b8), 0);
        lv_obj_set_style_pad_top(s, 4, 0);
    }
}

// POST a command to the BFF control API with the operator token. Returns HTTP
// status (or -1). Runs on cmd_worker, never on the LVGL/click stack.
static int http_post_cmd(const char *url, const char *body)
{
    esp_http_client_config_t cfg = { .url = url, .timeout_ms = 8000, .method = HTTP_METHOD_POST };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) return -1;
    esp_http_client_set_header(c, "Content-Type", "application/json");
    esp_http_client_set_header(c, "Authorization", "Bearer " PANEL_TOKEN);
    esp_http_client_set_post_field(c, body, strlen(body));
    int code = -1;
    if (esp_http_client_perform(c) == ESP_OK) code = esp_http_client_get_status_code(c);
    esp_http_client_cleanup(c);
    return code;
}

static void cmd_worker(void *pv)
{
    struct cmd_req req;
    for (;;) {
        if (xQueueReceive(s_cmd_q, &req, portMAX_DELAY) != pdTRUE) continue;
        char url[288];
        snprintf(url, sizeof(url), "%s/devices/%s/command", s_base, req.id);
        int code = http_post_cmd(url, req.body);
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

// ---- chart fetch worker: 72h readings per metric -> populate the expansion's charts ----
// Runs OFF the click/LVGL stack. Fetch happens unlocked; every s_exp[] touch is under the
// LVGL lock with an epoch re-check, so a close mid-fetch discards the result safely.
#define CHART_SCALE 100      // ints for lv_chart; preserves 2 decimals

// Populate chart #gi of slot from a fetched readings array. Holds the LVGL lock. Returns
// false if the expansion was closed/reused (epoch mismatch) — caller stops working it.
static bool chart_fill(int slot, uint32_t epoch, int gi, const int32_t *vals, int n,
                       int32_t vmin, int32_t vmax)
{
    if (!lvgl_port_lock(0)) return false;
    struct expand_ref *x = &s_exp[slot];
    bool ok = x->active && x->epoch == epoch && gi < x->ngraph && x->chart[gi] && x->ser[gi];
    if (ok) {
        lv_obj_t *ch = x->chart[gi];
        if (n < 1) {
            if (x->note[gi]) lv_label_set_text(x->note[gi], "no data (72h)");
        } else {
            int32_t lo = vmin, hi = vmax;
            if (lo == hi) { lo -= CHART_SCALE; hi += CHART_SCALE; }   // avoid a zero-height axis
            lv_chart_set_range(ch, LV_CHART_AXIS_PRIMARY_Y, lo, hi);
            lv_chart_set_point_count(ch, n);
            for (int i = 0; i < n; i++) lv_chart_set_value_by_id(ch, x->ser[gi], i, vals[i]);
            lv_chart_refresh(ch);
            if (x->note[gi]) {
                char nb[40];
                snprintf(nb, sizeof(nb), "%.2f – %.2f  ·  %d pts",
                         (double)vmin / CHART_SCALE, (double)vmax / CHART_SCALE, n);
                lv_label_set_text(x->note[gi], nb);
            }
        }
    }
    lvgl_port_unlock();
    return ok;
}

static void chart_worker(void *pv)
{
    struct chart_req rq;
    static int32_t vals[GRAPH_POINTS];
    for (;;) {
        if (xQueueReceive(s_chart_q, &rq, portMAX_DELAY) != pdTRUE) continue;
        // snapshot the request target under the lock (id + graph specs) so the unlocked
        // fetch can't tear against a concurrent close/reuse
        char id[40]; struct gspec g[MAX_GRAPHS]; int ngraph = 0;
        if (lvgl_port_lock(0)) {
            struct expand_ref *x = &s_exp[rq.slot];
            if (x->active && x->epoch == rq.epoch) {
                snprintf(id, sizeof(id), "%s", x->id);
                ngraph = x->ngraph;
                for (int i = 0; i < ngraph; i++) g[i] = x->g[i];
            }
            lvgl_port_unlock();
        }
        for (int i = 0; i < ngraph; i++) {
            char url[384];
            snprintf(url, sizeof(url), "%s/devices/%s/readings?metric=%s&hours=%d&limit=%d",
                     s_base, id, g[i].key, GRAPH_HOURS, GRAPH_POINTS);
            int len = 0; char *buf = http_get(url, &len);
            int n = 0; int32_t vmin = 0, vmax = 0;
            if (buf && len > 0) {
                cJSON *root = cJSON_Parse(buf);
                cJSON *arr = root ? cJSON_GetObjectItem(root, "readings") : NULL;
                if (cJSON_IsArray(arr)) {
                    cJSON *r;
                    cJSON_ArrayForEach(r, arr) {
                        if (n >= GRAPH_POINTS) break;
                        cJSON *v = cJSON_GetObjectItem(r, "value");
                        if (!cJSON_IsNumber(v)) continue;
                        int32_t sv = (int32_t)(v->valuedouble * CHART_SCALE);
                        vals[n] = sv;
                        if (n == 0 || sv < vmin) vmin = sv;
                        if (n == 0 || sv > vmax) vmax = sv;
                        n++;
                    }
                }
                if (root) cJSON_Delete(root);
            }
            if (buf) heap_caps_free(buf);
            if (!chart_fill(rq.slot, rq.epoch, i, vals, n, vmin, vmax))
                break;   // expansion closed mid-fetch — stop working this request
        }
    }
}

// ---- admin session + scene control (ADR-0019 Phase B) ----
// Send JSON (POST/PUT) with an optional bearer; capture the response body. Runs on admin_worker only.
static int http_send_json(esp_http_client_method_t method, const char *url, const char *body,
                          const char *bearer, char *resp, int resp_cap)
{
    esp_http_client_config_t cfg = { .url = url, .timeout_ms = 8000, .method = method };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) return -1;
    esp_http_client_set_header(c, "Content-Type", "application/json");
    if (bearer && bearer[0]) {
        char h[440];
        snprintf(h, sizeof(h), "Bearer %s", bearer);
        esp_http_client_set_header(c, "Authorization", h);
    }
    if (resp && resp_cap > 0) resp[0] = 0;
    int code = -1, blen = strlen(body);
    if (esp_http_client_open(c, blen) == ESP_OK) {
        esp_http_client_write(c, body, blen);
        esp_http_client_fetch_headers(c);
        code = esp_http_client_get_status_code(c);
        if (resp && resp_cap > 1) {
            int n = esp_http_client_read_response(c, resp, resp_cap - 1);
            if (n >= 0) resp[n] = 0;
        }
    }
    esp_http_client_close(c);
    esp_http_client_cleanup(c);
    return code;
}

static void toast(const char *msg)   // transient status line in the top bar (self-locks)
{
    if (lvgl_port_lock(0)) { if (s_toast) lv_label_set_text(s_toast, msg); lvgl_port_unlock(); }
}

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
            snprintf(url, sizeof(url), "%s/auth/login", s_base);
            char cred[65];
            api_token_hex(rq.arg, cred);                      // hash the typed passphrase (PWA-compatible)
            char body[128];
            snprintf(body, sizeof(body), "{\"password\":\"%s\"}", cred);
            int code = http_send_json(HTTP_METHOD_POST, url, body, NULL, resp, sizeof(resp));
            if (code == 200) {
                cJSON *r = cJSON_Parse(resp);
                cJSON *t = r ? cJSON_GetObjectItem(r, "token") : NULL;
                if (cJSON_IsString(t)) {
                    snprintf(s_admin_tok, sizeof(s_admin_tok), "%s", t->valuestring);
                    s_admin_active = true;
                    s_admin_last_us = esp_timer_get_time();
                    admin_paint_ui();
                    toast("admin unlocked");
                } else toast("login: no token");
                if (r) cJSON_Delete(r);
            } else toast(code == 401 ? "wrong password" : "login failed");
        } else {
            // all other admin actions require the RAM admin JWT
            if (!s_admin_active) { toast("unlock first"); continue; }
            const char *tok = s_admin_tok;
            int code = -1;
            if (rq.kind == 1) {                               // set whole-house scene
                snprintf(url, sizeof(url), "%s/control/house/scene", s_base);
                char body[460];
                snprintf(body, sizeof(body), "{\"scene\":\"%s\"}", rq.arg);
                code = http_send_json(HTTP_METHOD_POST, url, body, tok, NULL, 0);
                if (code == 200) toast("scene set");
            } else if (rq.kind == 2) {                        // manual override (arg = JSON body)
                snprintf(url, sizeof(url), "%s/control/%s/override", s_base, rq.id);
                code = http_send_json(HTTP_METHOD_POST, url, rq.arg, tok, NULL, 0);
                if (code == 200) toast("override applied");
            } else if (rq.kind == 3) {                        // automation policy edit (PUT, arg = JSON body)
                snprintf(url, sizeof(url), "%s/control/%s/policy", s_base, rq.id);
                code = http_send_json(HTTP_METHOD_PUT, url, rq.arg, tok, resp, sizeof(resp));
                if (code == 200) toast("automation saved");
            }
            if (code == 200) s_admin_last_us = esp_timer_get_time();
            else if (code == 401 || code == 403) { admin_relock(); toast("session expired — unlock"); }
            else if (code >= 400) toast("rejected by server");
            else toast("request failed");
        }
    }
}

// scene button: request the scene (admin only). Runs in LVGL/click ctx — enqueue only.
static void scene_btn_cb(lv_event_t *e)
{
    int idx = (int)(intptr_t)lv_event_get_user_data(e);
    if (idx < 0 || idx >= s_nscenes) return;
    if (!s_admin_active) { if (s_toast) lv_label_set_text(s_toast, "unlock (gear) to change scene"); return; }
    struct admin_req rq = { .kind = 1 };
    snprintf(rq.arg, sizeof(rq.arg), "%s", s_scene_name[idx]);
    if (s_admin_q && xQueueSend(s_admin_q, &rq, 0) == pdTRUE && s_toast)
        lv_label_set_text(s_toast, "setting scene…");
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

// Build the scene buttons once (spec-driven from /api/v1/house `scenes`) + refresh the active
// highlight each fetch. Called from ui_task; takes the LVGL lock itself.
static void render_house(cJSON *house)
{
    cJSON *scene = cJSON_GetObjectItem(house, "scene");
    cJSON *scenes = cJSON_GetObjectItem(house, "scenes");
    if (!lvgl_port_lock(0)) return;
    if (cJSON_IsString(scene)) snprintf(s_scene_active, sizeof(s_scene_active), "%s", scene->valuestring);
    if (s_nscenes == 0 && cJSON_IsArray(scenes) && s_scenebox) {
        cJSON *sc;
        cJSON_ArrayForEach(sc, scenes) {
            if (s_nscenes >= MAX_SCENES || !cJSON_IsString(sc)) break;
            int i = s_nscenes;
            snprintf(s_scene_name[i], sizeof(s_scene_name[i]), "%s", sc->valuestring);
            lv_obj_t *b = lv_button_create(s_scenebox);
            lv_obj_set_height(b, 46);
            lv_obj_add_event_cb(b, scene_btn_cb, LV_EVENT_CLICKED, (void *)(intptr_t)i);
            lv_obj_t *l = lv_label_create(b);
            lv_label_set_text(l, s_scene_name[i]);
            lv_obj_set_style_text_font(l, &lv_font_montserrat_20, 0);
            lv_obj_center(l);
            s_scene_btn[i] = b;
            s_nscenes++;
        }
    }
    for (int i = 0; i < s_nscenes; i++)          // highlight the active scene
        lv_obj_set_style_bg_color(s_scene_btn[i],
            lv_color_hex(strcmp(s_scene_name[i], s_scene_active) == 0 ? 0x2563eb : 0x1e293b), 0);
    lvgl_port_unlock();
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
    if (!s_admin_q || !s_cmd_target[0]) return;
    struct admin_req rq = { .kind = 2 };
    snprintf(rq.id, sizeof(rq.id), "%s", s_cmd_target);
    if (duration_min > 0)
        snprintf(rq.arg, sizeof(rq.arg), "{\"action\":\"%s\",\"duration_min\":%d}", action, duration_min);
    else
        snprintf(rq.arg, sizeof(rq.arg), "{\"action\":\"%s\"}", action);
    if (xQueueSend(s_admin_q, &rq, 0) == pdTRUE && s_cmd_result) lv_label_set_text(s_cmd_result, "applying…");
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
    if (!s_admin_q || !s_cmd_target[0]) return;
    struct admin_req rq = { .kind = 3 };
    snprintf(rq.id, sizeof(rq.id), "%s", s_cmd_target);
    snprintf(rq.arg, sizeof(rq.arg),
        "{\"enabled\":true,\"control\":{\"strategy\":\"hysteresis\",\"on_above\":%.0f,\"off_below\":%.0f}}",
        s_edit_on_above, s_edit_off_below);
    if (xQueueSend(s_admin_q, &rq, 0) == pdTRUE && s_cmd_result) lv_label_set_text(s_cmd_result, "saving…");
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
    if (s_admin_ctrls) {
        if (s_admin_active) lv_obj_clear_flag(s_admin_ctrls, LV_OBJ_FLAG_HIDDEN);
        else lv_obj_add_flag(s_admin_ctrls, LV_OBJ_FLAG_HIDDEN);
    }
    if (s_ov_autorow) {
        if (s_admin_active && s_ov_has_policy) lv_obj_clear_flag(s_ov_autorow, LV_OBJ_FLAG_HIDDEN);
        else lv_obj_add_flag(s_ov_autorow, LV_OBJ_FLAG_HIDDEN);
    }
    lv_obj_clear_flag(s_cmd_ov, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(s_cmd_ov);
}

// Read-only actuator/controllable-device card (from /api/v1/displays). Distinct
// amber accent; green border when running. Tap -> command overlay (if token set).
static void actuator_card(cJSON *d)
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

    lv_obj_t *card = lv_obj_create(s_grid);
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
            cJSON *st = cJSON_GetObjectItem(ctrl, "strategy");
            cJSON *en = cJSON_GetObjectItem(ctrl, "enabled");
            if (cJSON_IsString(st)) snprintf(ar->strategy, sizeof(ar->strategy), "%s", st->valuestring);
            if (cJSON_IsBool(en)) ar->enabled = cJSON_IsTrue(en);
            if (cJSON_IsNumber(oa) && cJSON_IsNumber(ob)) {   // hysteresis thresholds -> editable
                ar->on_above = oa->valuedouble; ar->off_below = ob->valuedouble; ar->has_policy = true;
            }
        }
        lv_obj_add_flag(card, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(card, act_clicked_cb, LV_EVENT_CLICKED, (void *)(intptr_t)idx);
    }
}

static void render(cJSON *sensors, cJSON *devices)
{
    if (!lvgl_port_lock(0)) return;
    for (int i = 0; i < s_ncards; i++) { free(s_cards[i].detail); s_cards[i].detail = NULL; }
    lv_obj_clean(s_grid);
    s_ncards = 0;                     // registries rebuilt from scratch each fetch
    s_nacts = 0;
    int ns = 0, na = 0;
    cJSON *e;
    if (devices) cJSON_ArrayForEach(e, devices) { actuator_card(e); na++; }   // actuators first (top)
    if (sensors) cJSON_ArrayForEach(e, sensors) { card_for(e); ns++; }
    lv_label_set_text_fmt(s_header, "Home  -  %d devices  %d sensors", na, ns);
    lvgl_port_unlock();
    ESP_LOGI(TAG, "rendered %d actuators + %d sensors", na, ns);
}

static void ui_task(void *pv)
{
    for (;;) {
        int l1 = 0, l2 = 0, l3 = 0;
        char *b1 = http_get(s_url, &l1);         // sensors
        char *b2 = http_get(s_disp_url, &l2);    // controllable devices
        char *b3 = http_get(s_house_url, &l3);   // whole-house scene state
        cJSON *r1 = (b1 && l1 > 0) ? cJSON_Parse(b1) : NULL;
        cJSON *r2 = (b2 && l2 > 0) ? cJSON_Parse(b2) : NULL;
        cJSON *r3 = (b3 && l3 > 0) ? cJSON_Parse(b3) : NULL;
        cJSON *sensors = r1 ? cJSON_GetObjectItem(r1, "sensors") : NULL;
        cJSON *devices = r2 ? cJSON_GetObjectItem(r2, "devices") : NULL;
        if (cJSON_IsArray(sensors) || cJSON_IsArray(devices)) {
            render(cJSON_IsArray(sensors) ? sensors : NULL, cJSON_IsArray(devices) ? devices : NULL);
        } else {
            ESP_LOGW(TAG, "fetch/parse failed");
            if (lvgl_port_lock(0)) { lv_label_set_text(s_header, "Home  -  (offline)"); lvgl_port_unlock(); }
        }
        if (cJSON_IsObject(r3)) render_house(r3);
        // idle auto-lock: drop the admin session after inactivity (relative esp_timer, no wall clock)
        if (s_admin_active && esp_timer_get_time() - s_admin_last_us > ADMIN_IDLE_US) {
            admin_relock();
            toast("admin auto-locked (idle)");
        }
        if (r1) cJSON_Delete(r1);
        if (r2) cJSON_Delete(r2);
        if (r3) cJSON_Delete(r3);
        if (b1) heap_caps_free(b1);
        if (b2) heap_caps_free(b2);
        if (b3) heap_caps_free(b3);
        vTaskDelay(pdMS_TO_TICKS(REFRESH_MS));
    }
}

// Parse one state payload and patch the matching card's headline (holds the LVGL
// lock). Runs on state_task — NEVER on the MQTT callback stack (LVGL is too heavy
// for it: the retained state burst would overflow the mqtt task and reboot).
static void apply_state(const char *json)
{
    cJSON *root = cJSON_Parse(json);
    if (!root) return;
    const cJSON *jid = cJSON_GetObjectItem(root, "device_id");
    cJSON *metrics = cJSON_GetObjectItem(root, "metrics");
    if (cJSON_IsString(jid) && cJSON_IsObject(metrics) && lvgl_port_lock(0)) {
        for (int i = 0; i < s_ncards; i++) {
            if (strcmp(s_cards[i].id, jid->valuestring) != 0) continue;
            bool p; double v = metric_of(metrics, s_cards[i].hkey, &p);
            if (p && s_cards[i].hval) {
                // NB: format with newlib snprintf, then lv_label_set_text — LVGL's
                // built-in printf does NOT support %f/%.*f and would crash (desync).
                char buf[48];
                snprintf(buf, sizeof(buf), "%s %.*f %s",
                         s_cards[i].hlabel, s_cards[i].hprec, v, s_cards[i].hunit);
                lv_label_set_text(s_cards[i].hval, buf);
            }
            break;
        }
        lvgl_port_unlock();
    }
    cJSON_Delete(root);
}

static void state_task(void *pv)
{
    char *json;
    for (;;) {
        if (xQueueReceive(s_state_q, &json, portMAX_DELAY) == pdTRUE) {
            apply_state(json);
            free(json);
        }
    }
}

// Called from the MQTT callback: LIGHTWEIGHT — copy + enqueue only, no parse/LVGL.
void ui_tiles_set_battery(int pct, bool charging)
{
    if (pct < 0) pct = 0;
    if (pct > 100) pct = 100;
    const char *sym = charging      ? LV_SYMBOL_CHARGE :
                      pct >= 88     ? LV_SYMBOL_BATTERY_FULL :
                      pct >= 63     ? LV_SYMBOL_BATTERY_3 :
                      pct >= 38     ? LV_SYMBOL_BATTERY_2 :
                      pct >= 13     ? LV_SYMBOL_BATTERY_1 : LV_SYMBOL_BATTERY_EMPTY;
    if (lvgl_port_lock(0)) {
        if (s_batt_lbl) lv_label_set_text_fmt(s_batt_lbl, "%s %d%%", sym, pct);
        lvgl_port_unlock();
    }
}

void ui_tiles_on_state(const char *json)
{
    if (!s_started || !s_state_q || !json) return;
    char *copy = strdup(json);
    if (!copy) return;
    if (xQueueSend(s_state_q, &copy, 0) != pdTRUE) free(copy);   // drop if backed up (stale anyway)
}

void ui_tiles_start(const char *sensors_url)
{
    strncpy(s_url, sensors_url, sizeof(s_url) - 1);
    snprintf(s_disp_url, sizeof(s_disp_url), "%s", s_url);      // sibling endpoint: /sensors -> /displays
    char *p = strstr(s_disp_url, "/sensors");
    if (p) strcpy(p, "/displays");
    snprintf(s_base, sizeof(s_base), "%s", s_url);             // BFF base for /devices/<id>/command
    char *b = strstr(s_base, "/api/v1");
    if (b) *b = 0;
    snprintf(s_house_url, sizeof(s_house_url), "%s/api/v1/house", s_base);
    s_started = true;

    if (!lvgl_port_lock(0)) { ESP_LOGE(TAG, "lvgl lock failed"); return; }
    lv_obj_t *scr = lv_scr_act();
    lv_obj_clean(scr);                        // drop the bring-up splash
    lv_obj_set_style_bg_color(scr, lv_color_hex(0x0b1021), 0);
    lv_obj_set_style_pad_all(scr, 10, 0);
    lv_obj_set_flex_flow(scr, LV_FLEX_FLOW_COLUMN);

    // ── top bar: [ scene buttons ]  … toast …  [ admin gear ] ──
    s_topbar = lv_obj_create(scr);
    lv_obj_set_width(s_topbar, lv_pct(100));
    lv_obj_set_height(s_topbar, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(s_topbar, 0, 0);
    lv_obj_set_style_border_width(s_topbar, 0, 0);
    lv_obj_set_style_pad_all(s_topbar, 0, 0);
    lv_obj_set_style_pad_bottom(s_topbar, 8, 0);
    lv_obj_set_flex_flow(s_topbar, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(s_topbar, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(s_topbar, LV_OBJ_FLAG_SCROLLABLE);

    s_scenebox = lv_obj_create(s_topbar);          // scene buttons (filled by render_house)
    lv_obj_set_height(s_scenebox, LV_SIZE_CONTENT);
    lv_obj_set_flex_grow(s_scenebox, 1);
    lv_obj_set_style_bg_opa(s_scenebox, 0, 0);
    lv_obj_set_style_border_width(s_scenebox, 0, 0);
    lv_obj_set_style_pad_all(s_scenebox, 0, 0);
    lv_obj_set_style_pad_column(s_scenebox, 8, 0);
    lv_obj_set_flex_flow(s_scenebox, LV_FLEX_FLOW_ROW);
    lv_obj_clear_flag(s_scenebox, LV_OBJ_FLAG_SCROLLABLE);

    s_toast = lv_label_create(s_topbar);           // transient status
    lv_label_set_text(s_toast, "");
    lv_obj_set_style_text_color(s_toast, lv_color_hex(0x8fb4ff), 0);
    lv_obj_set_style_pad_right(s_toast, 10, 0);

    s_batt_lbl = lv_label_create(s_topbar);        // battery indicator (MAX17048)
    lv_label_set_text(s_batt_lbl, "");
    lv_obj_set_style_text_font(s_batt_lbl, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(s_batt_lbl, lv_color_hex(0xcbd5e1), 0);
    lv_obj_set_style_pad_right(s_batt_lbl, 10, 0);

    s_admin_btn = lv_button_create(s_topbar);      // lock/unlock gear
    lv_obj_set_height(s_admin_btn, 46);
    lv_obj_set_style_bg_color(s_admin_btn, lv_color_hex(0x334155), 0);
    lv_obj_add_event_cb(s_admin_btn, admin_btn_cb, LV_EVENT_CLICKED, NULL);
    s_admin_lbl = lv_label_create(s_admin_btn);
    lv_label_set_text(s_admin_lbl, LV_SYMBOL_SETTINGS " Locked");
    lv_obj_set_style_text_font(s_admin_lbl, &lv_font_montserrat_20, 0);
    lv_obj_center(s_admin_lbl);

    s_header = lv_label_create(scr);
    lv_label_set_text(s_header, "Home  -  loading...");
    lv_obj_set_style_text_font(s_header, &lv_font_montserrat_28, 0);
    lv_obj_set_style_text_color(s_header, lv_color_hex(0xffffff), 0);
    lv_obj_set_style_pad_bottom(s_header, 6, 0);

    // The screen itself scrolls vertically: [header] -> [tile grid] -> [expansion stack].
    lv_obj_set_scrollbar_mode(scr, LV_SCROLLBAR_MODE_AUTO);
    lv_obj_set_scroll_dir(scr, LV_DIR_VER);

    s_grid = lv_obj_create(scr);
    lv_obj_set_width(s_grid, lv_pct(100));
    lv_obj_set_height(s_grid, LV_SIZE_CONTENT);      // size to its rows so expansions can stack below
    lv_obj_set_style_bg_opa(s_grid, 0, 0);
    lv_obj_set_style_border_width(s_grid, 0, 0);
    lv_obj_set_style_pad_all(s_grid, 0, 0);
    lv_obj_set_style_pad_row(s_grid, 10, 0);
    lv_obj_set_style_pad_column(s_grid, 10, 0);
    lv_obj_set_flex_flow(s_grid, LV_FLEX_FLOW_ROW_WRAP);
    lv_obj_clear_flag(s_grid, LV_OBJ_FLAG_SCROLLABLE);   // the screen scrolls, not the grid

    // expansion stack: full-width inline panels appended below the grid (ADR-0019 Phase A)
    s_expbox = lv_obj_create(scr);
    lv_obj_set_width(s_expbox, lv_pct(100));
    lv_obj_set_height(s_expbox, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_opa(s_expbox, 0, 0);
    lv_obj_set_style_border_width(s_expbox, 0, 0);
    lv_obj_set_style_pad_all(s_expbox, 0, 0);
    lv_obj_set_style_pad_top(s_expbox, 10, 0);
    lv_obj_set_style_pad_row(s_expbox, 10, 0);
    lv_obj_set_flex_flow(s_expbox, LV_FLEX_FLOW_COLUMN);
    lv_obj_clear_flag(s_expbox, LV_OBJ_FLAG_SCROLLABLE);

    // actuator command overlay (only if we hold an operator token)
    if (HAVE_TOKEN) {
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
    }

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

    lvgl_port_unlock();

    s_state_q = xQueueCreate(24, sizeof(char *));   // MQTT state payloads
    xTaskCreate(state_task, "uistate", 6144, NULL, 4, NULL);
    xTaskCreate(ui_task, "ui", 8192, NULL, 4, NULL);
    s_chart_q = xQueueCreate(4, sizeof(struct chart_req));   // expansion chart fetches
    xTaskCreate(chart_worker, "uichart", 8192, NULL, 4, NULL);
    s_admin_q = xQueueCreate(4, sizeof(struct admin_req));   // login + scene-set (admin)
    xTaskCreate(admin_worker, "uiadmin", 8192, NULL, 4, NULL);
    if (HAVE_TOKEN) {
        s_cmd_q = xQueueCreate(8, sizeof(struct cmd_req));
        xTaskCreate(cmd_worker, "uicmd", 6144, NULL, 4, NULL);
    }
    ESP_LOGI(TAG, "ui_tiles started -> %s", s_url);
}
