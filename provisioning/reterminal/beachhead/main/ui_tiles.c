// Server-backed LVGL tile renderer (ADR-0019 Phase 2). See ui_tiles.h.
#include "ui_tiles.h"
#include <string.h>
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
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
    lv_obj_clean(s_grid);
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

void ui_tiles_start(const char *sensors_url)
{
    strncpy(s_url, sensors_url, sizeof(s_url) - 1);

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
    lvgl_port_unlock();

    xTaskCreate(ui_task, "ui", 8192, NULL, 4, NULL);
    ESP_LOGI(TAG, "ui_tiles started -> %s", s_url);
}
