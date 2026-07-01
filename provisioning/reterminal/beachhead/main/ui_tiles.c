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
#include "cJSON.h"
#include "lvgl.h"
#include "esp_lvgl_port.h"

static const char *TAG = "ui";
#define REFRESH_MS 10000

static char s_url[192];
static lv_obj_t *s_header, *s_grid;

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
};
#define MAX_CARDS 48
static struct card_ref s_cards[MAX_CARDS];
static int s_ncards;
static bool s_started;
static QueueHandle_t s_state_q;    // MQTT state payloads (char*) -> state_task (LVGL off the mqtt stack)
static lv_obj_t *s_detail, *s_detail_title, *s_detail_body;   // tap-to-open detail overlay

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

// ---- tap-to-open detail overlay ----
static void detail_close_cb(lv_event_t *e) { (void)e; lv_obj_add_flag(s_detail, LV_OBJ_FLAG_HIDDEN); }

static void card_clicked_cb(lv_event_t *e)
{
    int idx = (int)(intptr_t)lv_event_get_user_data(e);
    if (idx < 0 || idx >= s_ncards || !s_detail) return;
    struct card_ref *c = &s_cards[idx];
    ESP_LOGI(TAG, "tap -> card %d (%s)", idx, c->id);   // validates touch coords hit the right card
    lv_label_set_text(s_detail_title, c->name[0] ? c->name : c->id);
    char body[420];
    snprintf(body, sizeof(body), "%s\n\n%s", c->room, c->detail ? c->detail : "");
    lv_label_set_text(s_detail_body, body);
    lv_obj_clear_flag(s_detail, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(s_detail);
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

static void render(cJSON *sensors)
{
    if (!lvgl_port_lock(0)) return;
    for (int i = 0; i < s_ncards; i++) { free(s_cards[i].detail); s_cards[i].detail = NULL; }
    lv_obj_clean(s_grid);
    s_ncards = 0;                     // registry rebuilt from scratch each fetch
    int n = 0;
    cJSON *e;
    cJSON_ArrayForEach(e, sensors) { card_for(e); n++; }
    lv_label_set_text_fmt(s_header, "Home  -  %d sensors", n);
    lvgl_port_unlock();
    ESP_LOGI(TAG, "rendered %d cards", n);
}

static void ui_task(void *pv)
{
    for (;;) {
        int len = 0;
        char *body = http_get(s_url, &len);
        if (body && len > 0) {
            cJSON *root = cJSON_Parse(body);
            if (root) {
                cJSON *sensors = cJSON_GetObjectItem(root, "sensors");
                if (cJSON_IsArray(sensors)) render(sensors);
                cJSON_Delete(root);
            } else {
                ESP_LOGW(TAG, "JSON parse failed (%d bytes)", len);
            }
        } else {
            ESP_LOGW(TAG, "fetch failed: %s", s_url);
            if (lvgl_port_lock(0)) { lv_label_set_text(s_header, "Home  -  (offline)"); lvgl_port_unlock(); }
        }
        if (body) heap_caps_free(body);
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
    s_started = true;

    if (!lvgl_port_lock(0)) { ESP_LOGE(TAG, "lvgl lock failed"); return; }
    lv_obj_t *scr = lv_scr_act();
    lv_obj_clean(scr);                        // drop the bring-up splash
    lv_obj_set_style_bg_color(scr, lv_color_hex(0x0b1021), 0);
    lv_obj_set_style_pad_all(scr, 10, 0);
    lv_obj_set_flex_flow(scr, LV_FLEX_FLOW_COLUMN);

    s_header = lv_label_create(scr);
    lv_label_set_text(s_header, "Home  -  loading...");
    lv_obj_set_style_text_font(s_header, &lv_font_montserrat_28, 0);
    lv_obj_set_style_text_color(s_header, lv_color_hex(0xffffff), 0);
    lv_obj_set_style_pad_bottom(s_header, 6, 0);

    s_grid = lv_obj_create(scr);
    lv_obj_set_width(s_grid, lv_pct(100));
    lv_obj_set_flex_grow(s_grid, 1);
    lv_obj_set_style_bg_opa(s_grid, 0, 0);
    lv_obj_set_style_border_width(s_grid, 0, 0);
    lv_obj_set_style_pad_all(s_grid, 0, 0);
    lv_obj_set_style_pad_row(s_grid, 10, 0);
    lv_obj_set_style_pad_column(s_grid, 10, 0);
    lv_obj_set_flex_flow(s_grid, LV_FLEX_FLOW_ROW_WRAP);

    // tap-to-open detail overlay on the top layer (modal; hidden until a tap)
    s_detail = lv_obj_create(lv_layer_top());
    lv_obj_set_size(s_detail, lv_pct(100), lv_pct(100));
    lv_obj_set_style_bg_color(s_detail, lv_color_hex(0x0b1021), 0);
    lv_obj_set_style_bg_opa(s_detail, LV_OPA_COVER, 0);
    lv_obj_set_style_border_width(s_detail, 0, 0);
    lv_obj_set_style_radius(s_detail, 0, 0);
    lv_obj_set_style_pad_all(s_detail, 36, 0);
    lv_obj_set_flex_flow(s_detail, LV_FLEX_FLOW_COLUMN);
    lv_obj_add_flag(s_detail, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(s_detail, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(s_detail, detail_close_cb, LV_EVENT_CLICKED, NULL);

    s_detail_title = lv_label_create(s_detail);
    lv_obj_set_style_text_font(s_detail_title, &lv_font_montserrat_28, 0);
    lv_obj_set_style_text_color(s_detail_title, lv_color_hex(0xffffff), 0);
    lv_obj_t *hint = lv_label_create(s_detail);
    lv_label_set_text(hint, "tap anywhere to close");
    lv_obj_set_style_text_color(hint, lv_color_hex(0x64748b), 0);
    lv_obj_set_style_pad_bottom(hint, 14, 0);
    s_detail_body = lv_label_create(s_detail);
    lv_obj_set_style_text_font(s_detail_body, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(s_detail_body, lv_color_hex(0xcbd5e1), 0);

    lvgl_port_unlock();

    s_state_q = xQueueCreate(24, sizeof(char *));   // MQTT state payloads
    xTaskCreate(state_task, "uistate", 6144, NULL, 4, NULL);
    xTaskCreate(ui_task, "ui", 8192, NULL, 4, NULL);
    ESP_LOGI(TAG, "ui_tiles started -> %s", s_url);
}
