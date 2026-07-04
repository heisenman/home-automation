// Runtime side of the versioned battery profile — see ha_battery_profile_rt.h.
#include "ha_battery_profile_rt.h"

#include <string.h>
#include <stdio.h>

#include "nvs.h"
#include "cJSON.h"
#include "esp_log.h"

static const char *TAG = "battprof_rt";

#define NVS_NS   "batt"        // NVS namespace
#define NVS_KEY  "profile"     // blob key: the raw ha_batt_profile_t (POD)

esp_err_t ha_batt_profile_rt_load(ha_batt_profile_t *out)
{
    if (!out) return ESP_ERR_INVALID_ARG;
    nvs_handle_t h;
    esp_err_t e = nvs_open(NVS_NS, NVS_READONLY, &h);
    if (e != ESP_OK) return e;                 // NOT_FOUND when the namespace has never been written
    ha_batt_profile_t p;
    size_t len = sizeof(p);
    e = nvs_get_blob(h, NVS_KEY, &p, &len);
    nvs_close(h);
    if (e != ESP_OK) return e;                 // NOT_FOUND when no profile stored
    if (len != sizeof(p) || !ha_batt_profile_valid(&p)) {
        ESP_LOGW(TAG, "stored profile invalid (len=%d/%d) — ignoring, using default",
                 (int)len, (int)sizeof(p));
        return ESP_ERR_INVALID_STATE;
    }
    *out = p;
    ESP_LOGW(TAG, "loaded profile %s (%s / %s) from NVS", p.version, p.date, p.method);
    return ESP_OK;
}

esp_err_t ha_batt_profile_rt_save(const ha_batt_profile_t *p)
{
    if (!ha_batt_profile_valid(p)) return ESP_ERR_INVALID_ARG;
    nvs_handle_t h;
    esp_err_t e = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (e != ESP_OK) return e;
    e = nvs_set_blob(h, NVS_KEY, p, sizeof(*p));
    if (e == ESP_OK) e = nvs_commit(h);
    nvs_close(h);
    if (e == ESP_OK) ESP_LOGW(TAG, "persisted profile %s to NVS", p->version);
    return e;
}

esp_err_t ha_batt_profile_rt_clear(void)
{
    nvs_handle_t h;
    esp_err_t e = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (e != ESP_OK) return e;
    e = nvs_erase_key(h, NVS_KEY);
    if (e == ESP_ERR_NVS_NOT_FOUND) e = ESP_OK;   // already absent — fine
    if (e == ESP_OK) e = nvs_commit(h);
    nvs_close(h);
    return e;
}

static void copy_str(char *dst, int cap, const cJSON *s)
{
    if (cJSON_IsString(s) && s->valuestring) {
        strncpy(dst, s->valuestring, cap - 1);
        dst[cap - 1] = '\0';
    }
}

static int get_int(const cJSON *o, const char *key)
{
    const cJSON *j = cJSON_GetObjectItem(o, key);
    return cJSON_IsNumber(j) ? (int)j->valuedouble : 0;
}

esp_err_t ha_batt_profile_rt_from_json(const char *json, ha_batt_profile_t *out, char *err, int err_cap)
{
    #define FAIL(msg) do { if (err) snprintf(err, err_cap, "%s", (msg)); \
                           if (root) { cJSON_Delete(root); } \
                           return ESP_FAIL; } while (0)
    cJSON *root = NULL;
    if (!json || !out) FAIL("null arg");
    root = cJSON_Parse(json);
    if (!root) FAIL("bad json");

    ha_batt_profile_t p;
    memset(&p, 0, sizeof(p));
    copy_str(p.version, sizeof(p.version), cJSON_GetObjectItem(root, "version"));
    copy_str(p.date,    sizeof(p.date),    cJSON_GetObjectItem(root, "date"));
    copy_str(p.method,  sizeof(p.method),  cJSON_GetObjectItem(root, "method"));
    p.off_display_off_mv = get_int(root, "off_display_off_mv");
    p.off_usb_mv         = get_int(root, "off_usb_mv");
    p.off_charging_mv    = get_int(root, "off_charging_mv");
    p.run_floor_mv    = get_int(root, "run_floor_mv");
    p.warn_mv         = get_int(root, "warn_mv");
    p.warn_clear_mv   = get_int(root, "warn_clear_mv");
    p.boot_gate_mv    = get_int(root, "boot_gate_mv");
    p.boot_release_mv = get_int(root, "boot_release_mv");

    const cJSON *lut = cJSON_GetObjectItem(root, "lut");
    if (!cJSON_IsArray(lut)) FAIL("lut missing/not-array");
    int n = cJSON_GetArraySize(lut);
    if (n < 2 || n > HA_BATT_PROFILE_LUT_MAX) FAIL("lut_n out of range");
    for (int i = 0; i < n; i++) {
        const cJSON *v = cJSON_GetArrayItem(lut, i);
        if (!cJSON_IsNumber(v)) FAIL("lut has a non-number");
        p.lut[i] = (int)v->valuedouble;
    }
    p.lut_n = n;

    cJSON_Delete(root);
    root = NULL;
    if (!ha_batt_profile_valid(&p)) FAIL("failed validation (monotonic LUT / ordered floors / version)");
    *out = p;
    return ESP_OK;
    #undef FAIL
}

int ha_batt_profile_rt_to_json(const ha_batt_profile_t *p, const char *source, char *buf, int cap)
{
    if (!p || !buf) return -1;
    cJSON *r = cJSON_CreateObject();
    if (!r) return -1;
    cJSON_AddStringToObject(r, "version", p->version);
    cJSON_AddStringToObject(r, "date", p->date);
    cJSON_AddStringToObject(r, "method", p->method);
    if (source) cJSON_AddStringToObject(r, "source", source);
    cJSON_AddNumberToObject(r, "off_display_off_mv", p->off_display_off_mv);
    cJSON_AddNumberToObject(r, "off_usb_mv", p->off_usb_mv);
    cJSON_AddNumberToObject(r, "off_charging_mv", p->off_charging_mv);
    cJSON_AddNumberToObject(r, "run_floor_mv", p->run_floor_mv);
    cJSON_AddNumberToObject(r, "warn_mv", p->warn_mv);
    cJSON_AddNumberToObject(r, "warn_clear_mv", p->warn_clear_mv);
    cJSON_AddNumberToObject(r, "boot_gate_mv", p->boot_gate_mv);
    cJSON_AddNumberToObject(r, "boot_release_mv", p->boot_release_mv);
    cJSON *lut = cJSON_AddArrayToObject(r, "lut");
    for (int i = 0; i < p->lut_n; i++) cJSON_AddItemToArray(lut, cJSON_CreateNumber(p->lut[i]));

    char *s = cJSON_PrintUnformatted(r);
    int len = -1;
    if (s) {
        len = snprintf(buf, cap, "%s", s);
        cJSON_free(s);
    }
    cJSON_Delete(r);
    return len;
}
