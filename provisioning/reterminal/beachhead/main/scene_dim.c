// Device-local per-scene backlight policy — see scene_dim.h. Pure policy: a small scene->percent
// table with a baked-in default, an MQTT-pushable override, and NVS persistence. No LVGL / no I2C.
#include "scene_dim.h"
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include "cJSON.h"
#include "esp_log.h"
#include "nvs.h"

static const char *TAG = "scenedim";
#define NVS_NS   "scenedim"
#define NVS_KEY  "table"        // the override table, stored as a JSON object string
#define MAX_ENT  6

typedef struct { char name[16]; int pct; } scene_ent_t;
static scene_ent_t s_tab[MAX_ENT];
static int  s_n;
static char s_source[12] = "default";   // "default" | "nvs" | "pushed"

// The baked-in default policy: lit at Home, dimmed when Away, dark when asleep.
static void load_default(void)
{
    // Sleep is a FAINT level, not 0: the screen stays alive enough to show the on-screen "Wake" button
    // (ui_sleep). True screen-off is the physical back button. Home/Away are the lit/dimmed daytime levels.
    static const scene_ent_t def[] = {
        { "Home", 100 }, { "Away", 40 }, { "Sleep", 5 },
    };
    s_n = sizeof(def) / sizeof(def[0]);
    memcpy(s_tab, def, sizeof(def));
    snprintf(s_source, sizeof(s_source), "default");
}

// Parse a JSON object {"<scene>": <pct>, ...} into the table. Returns ESP_OK, or ESP_ERR_INVALID_ARG
// (with *err) on a non-object, empty, over-long, or out-of-range payload. Does not touch NVS.
static esp_err_t parse_table(const char *json, char *err, size_t errlen)
{
    cJSON *root = cJSON_Parse(json);
    if (!cJSON_IsObject(root)) { if (err) snprintf(err, errlen, "not a json object"); cJSON_Delete(root); return ESP_ERR_INVALID_ARG; }
    scene_ent_t tmp[MAX_ENT]; int n = 0;
    cJSON *e;
    cJSON_ArrayForEach(e, root) {
        if (!cJSON_IsNumber(e) || !e->string) { if (err) snprintf(err, errlen, "'%s' not a number", e->string ? e->string : "?"); cJSON_Delete(root); return ESP_ERR_INVALID_ARG; }
        if (n >= MAX_ENT) { if (err) snprintf(err, errlen, "too many scenes (max %d)", MAX_ENT); cJSON_Delete(root); return ESP_ERR_INVALID_ARG; }
        int pct = e->valueint;
        if (pct < 0 || pct > 100) { if (err) snprintf(err, errlen, "'%s'=%d out of 0..100", e->string, pct); cJSON_Delete(root); return ESP_ERR_INVALID_ARG; }
        strncpy(tmp[n].name, e->string, sizeof(tmp[n].name) - 1);
        tmp[n].name[sizeof(tmp[n].name) - 1] = 0;
        tmp[n].pct = pct;
        n++;
    }
    cJSON_Delete(root);
    if (n == 0) { if (err) snprintf(err, errlen, "empty table"); return ESP_ERR_INVALID_ARG; }
    memcpy(s_tab, tmp, sizeof(tmp));
    s_n = n;
    return ESP_OK;
}

static void nvs_save(const char *json)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    if (json) nvs_set_str(h, NVS_KEY, json); else nvs_erase_key(h, NVS_KEY);
    nvs_commit(h);
    nvs_close(h);
}

void scene_dim_init(void)
{
    load_default();
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        size_t len = 0;
        if (nvs_get_str(h, NVS_KEY, NULL, &len) == ESP_OK && len > 0 && len < 512) {
            char *buf = malloc(len);
            if (buf && nvs_get_str(h, NVS_KEY, buf, &len) == ESP_OK) {
                char err[48];
                if (parse_table(buf, err, sizeof(err)) == ESP_OK)
                    snprintf(s_source, sizeof(s_source), "nvs");
                else
                    ESP_LOGW(TAG, "stored table invalid (%s) -> default", err);
            }
            free(buf);
        }
        nvs_close(h);
    }
    ESP_LOGI(TAG, "init: %d scenes (%s)", s_n, s_source);
}

int scene_dim_lookup(const char *scene)
{
    if (!scene) return -1;
    for (int i = 0; i < s_n; i++)
        if (strcmp(s_tab[i].name, scene) == 0) return s_tab[i].pct;
    return -1;
}

esp_err_t scene_dim_set_from_json(const char *json, char *err, size_t errlen)
{
    esp_err_t r = parse_table(json, err, errlen);
    if (r != ESP_OK) return r;
    snprintf(s_source, sizeof(s_source), "pushed");
    nvs_save(json);
    ESP_LOGI(TAG, "pushed: %d scenes persisted", s_n);
    return ESP_OK;
}

void scene_dim_reset_default(void)
{
    load_default();
    nvs_save(NULL);
    ESP_LOGI(TAG, "reset to default (%d scenes)", s_n);
}

int scene_dim_to_json(char *buf, size_t buflen)
{
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "source", s_source);
    cJSON *t = cJSON_AddObjectToObject(root, "table");
    for (int i = 0; i < s_n; i++) cJSON_AddNumberToObject(t, s_tab[i].name, s_tab[i].pct);
    int n = cJSON_PrintPreallocated(root, buf, buflen, false) ? (int)strlen(buf) : 0;
    cJSON_Delete(root);
    return n;
}
