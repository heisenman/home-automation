// Sensors list view (see ui_sensors.h). Rebuilt on every refresh (10 s) from /api/v1/panel/sensors; the
// scroll position is carried across the rebuild. Fonts compiled: montserrat 14/20/28 only.
#include "ui/ui_sensors.h"
#include "ui/ui_format.h"   // ascii_fold, aq_band_color, disp_val
#include <stdio.h>
#include <string.h>
#include "esp_log.h"
#include "nvs.h"

static const char *TAG = "ui.sensors";

#define NVS_NS      "senslist"
#define NVS_KEY     "hidden"
#define MAX_HIDDEN  64
#define ID_LEN      40
#define STALE_S     900            // older than this -> values drawn dim (sensor quiet / offline)

// column widths (800 px portrait panel, 10 px screen inset): name | temp | hum | dew | air | notable | button
#define W_NAME  230
#define W_TEMP   88
#define W_HUM    66
#define W_DEW    88
#define W_AIR   112
#define W_BTN    56

#define C_ROW     0x111834
#define C_TEXT    0xffffff
#define C_DIM     0x64748b
#define C_HEAD    0x8fb4ff
#define C_BTN     0x16204a
#define C_EDGE    0x2f7e7a

static char s_hidden[MAX_HIDDEN][ID_LEN];
static int s_nhidden;
static bool s_show_hidden;          // "Hidden (n)" toggle: show hidden rows (dimmed) with an un-hide button
static void (*s_changed)(void);

// per-row button payloads (device ids) — stable storage for the click user_data across one render
#define MAX_ROWS 96
static char s_row_id[MAX_ROWS][ID_LEN];

// ── hidden set (NVS: comma-separated device ids) ─────────────────────────────
static int find_hidden(const char *id)
{
    for (int i = 0; i < s_nhidden; i++) if (strcmp(s_hidden[i], id) == 0) return i;
    return -1;
}

static void save_hidden(void)
{
    char buf[MAX_HIDDEN * ID_LEN];
    int n = 0;
    buf[0] = 0;
    for (int i = 0; i < s_nhidden && n < (int)sizeof buf - ID_LEN - 1; i++)
        n += snprintf(buf + n, sizeof buf - n, "%s%s", i ? "," : "", s_hidden[i]);
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) { ESP_LOGW(TAG, "nvs open failed"); return; }
    if (s_nhidden) nvs_set_str(h, NVS_KEY, buf); else nvs_erase_key(h, NVS_KEY);
    nvs_commit(h);
    nvs_close(h);
}

void ui_sensors_init(void)
{
    s_nhidden = 0;
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) return;
    char buf[MAX_HIDDEN * ID_LEN];
    size_t len = sizeof buf;
    if (nvs_get_str(h, NVS_KEY, buf, &len) == ESP_OK) {
        char *save = NULL;
        for (char *t = strtok_r(buf, ",", &save); t && s_nhidden < MAX_HIDDEN; t = strtok_r(NULL, ",", &save))
            snprintf(s_hidden[s_nhidden++], ID_LEN, "%s", t);
    }
    nvs_close(h);
    ESP_LOGI(TAG, "%d hidden sensor(s)", s_nhidden);
}

// ── clicks (LVGL lock held) ──────────────────────────────────────────────────
static void on_hide_toggle(lv_event_t *e)
{
    const char *id = lv_event_get_user_data(e);
    int i = find_hidden(id);
    if (i >= 0) {                                       // un-hide
        s_nhidden--;
        if (i != s_nhidden) memcpy(s_hidden[i], s_hidden[s_nhidden], ID_LEN);
    } else if (s_nhidden < MAX_HIDDEN) {                // hide
        snprintf(s_hidden[s_nhidden++], ID_LEN, "%s", id);
    }
    save_hidden();
    ESP_LOGI(TAG, "%s %s (%d hidden)", i >= 0 ? "unhide" : "hide", id, s_nhidden);
    if (s_changed) s_changed();
}

static void on_show_hidden(lv_event_t *e)
{
    (void)e;
    s_show_hidden = !s_show_hidden;
    if (s_changed) s_changed();
}

// ── widgets ──────────────────────────────────────────────────────────────────
static lv_obj_t *row_box(lv_obj_t *parent, bool card)
{
    lv_obj_t *r = lv_obj_create(parent);
    lv_obj_set_width(r, lv_pct(100));
    lv_obj_set_height(r, LV_SIZE_CONTENT);
    lv_obj_set_style_bg_color(r, lv_color_hex(C_ROW), 0);
    lv_obj_set_style_bg_opa(r, card ? LV_OPA_COVER : 0, 0);
    lv_obj_set_style_border_width(r, 0, 0);
    lv_obj_set_style_radius(r, 8, 0);
    lv_obj_set_style_pad_all(r, card ? 6 : 0, 0);
    lv_obj_set_style_pad_left(r, 8, 0);
    lv_obj_set_style_pad_column(r, 6, 0);
    lv_obj_set_flex_flow(r, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(r, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_clear_flag(r, LV_OBJ_FLAG_SCROLLABLE);
    return r;
}

static lv_obj_t *cell(lv_obj_t *row, const char *txt, int w, const lv_font_t *font, uint32_t col, bool right)
{
    lv_obj_t *l = lv_label_create(row);
    lv_label_set_text(l, txt);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(col), 0);
    lv_label_set_long_mode(l, LV_LABEL_LONG_DOT);
    if (w > 0) lv_obj_set_width(l, w); else { lv_obj_set_width(l, 1); lv_obj_set_flex_grow(l, 1); }
    if (right) lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_RIGHT, 0);
    return l;
}

static lv_obj_t *button(lv_obj_t *parent, const char *txt, int w, lv_event_cb_t cb, void *ud)
{
    lv_obj_t *b = lv_obj_create(parent);
    lv_obj_set_size(b, w, 40);
    lv_obj_set_style_bg_color(b, lv_color_hex(C_BTN), 0);
    lv_obj_set_style_border_color(b, lv_color_hex(C_EDGE), 0);
    lv_obj_set_style_border_width(b, 2, 0);
    lv_obj_set_style_radius(b, 8, 0);
    lv_obj_set_style_pad_all(b, 0, 0);
    lv_obj_clear_flag(b, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, ud);
    lv_obj_t *l = lv_label_create(b);
    lv_label_set_text(l, txt);
    lv_obj_set_style_text_font(l, &lv_font_montserrat_20, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(C_TEXT), 0);
    lv_obj_center(l);
    return b;
}

static void fmt_temp(cJSON *j, char *out, size_t cap)
{
    if (cJSON_IsNumber(j)) snprintf(out, cap, "%.1fF", disp_val("C", j->valuedouble));
    else snprintf(out, cap, "-");
}

// ── render ───────────────────────────────────────────────────────────────────
void ui_sensors_render(cJSON *doc, lv_obj_t *parent, void (*changed)(void))
{
    s_changed = changed;
    lv_coord_t y = lv_obj_get_scroll_y(parent);
    lv_obj_clean(parent);

    cJSON *rows = doc ? cJSON_GetObjectItem(doc, "sensors") : NULL;
    if (!cJSON_IsArray(rows)) {
        cell(row_box(parent, false), "No sensor data (offline?)", 0, &lv_font_montserrat_20, C_DIM, false);
        return;
    }

    // count the hidden ones that still exist (a hidden id whose sensor is gone doesn't count)
    int total = 0, nhid = 0;
    cJSON *e;
    cJSON_ArrayForEach(e, rows) {
        cJSON *jid = cJSON_GetObjectItem(e, "device_id");
        if (!cJSON_IsString(jid)) continue;
        total++;
        if (find_hidden(jid->valuestring) >= 0) nhid++;
    }

    // toolbar: [ n shown ]  ...  [ Hidden (n) ] — the way back to sensors someone hid
    lv_obj_t *bar = row_box(parent, false);
    char t[64];
    snprintf(t, sizeof t, "%d sensors%s", total - nhid, nhid ? "" : "  -  tap " LV_SYMBOL_EYE_CLOSE " to hide one");
    cell(bar, t, 0, &lv_font_montserrat_20, C_DIM, false);
    if (nhid || s_show_hidden) {
        snprintf(t, sizeof t, s_show_hidden ? LV_SYMBOL_EYE_CLOSE "  Done" : LV_SYMBOL_EYE_OPEN "  Hidden (%d)", nhid);
        button(bar, t, 200, on_show_hidden, NULL);
    }

    // column header
    lv_obj_t *hdr = row_box(parent, false);
    cell(hdr, "Sensor", W_NAME, &lv_font_montserrat_14, C_HEAD, false);
    cell(hdr, "Temp", W_TEMP, &lv_font_montserrat_14, C_HEAD, true);
    cell(hdr, "Hum", W_HUM, &lv_font_montserrat_14, C_HEAD, true);
    cell(hdr, "Dew", W_DEW, &lv_font_montserrat_14, C_HEAD, true);
    cell(hdr, "Air", W_AIR, &lv_font_montserrat_14, C_HEAD, true);
    cell(hdr, "Other", 0, &lv_font_montserrat_14, C_HEAD, false);
    lv_obj_t *pad = lv_label_create(hdr); lv_label_set_text(pad, ""); lv_obj_set_width(pad, W_BTN);

    char room[ID_LEN] = "";
    int nrow = 0;
    cJSON_ArrayForEach(e, rows) {
        cJSON *jid = cJSON_GetObjectItem(e, "device_id");
        if (!cJSON_IsString(jid) || nrow >= MAX_ROWS) continue;
        bool hidden = find_hidden(jid->valuestring) >= 0;
        if (hidden && !s_show_hidden) continue;

        cJSON *jroom = cJSON_GetObjectItem(e, "room");
        if (cJSON_IsString(jroom) && strcmp(room, jroom->valuestring) != 0) {      // room heading
            snprintf(room, sizeof room, "%s", jroom->valuestring);
            cJSON *jrn = cJSON_GetObjectItem(e, "room_name");
            char rn[40];
            ascii_fold(cJSON_IsString(jrn) ? jrn->valuestring : room, rn, sizeof rn);
            lv_obj_t *rh = row_box(parent, false);
            lv_obj_set_style_pad_top(rh, 6, 0);
            cell(rh, rn, 0, &lv_font_montserrat_14, C_HEAD, false);
        }

        cJSON *jage = cJSON_GetObjectItem(e, "age_s");
        bool stale = cJSON_IsNumber(jage) && jage->valuedouble > STALE_S;
        uint32_t vc = stale ? C_DIM : C_TEXT;
        lv_obj_t *r = row_box(parent, true);
        if (hidden) lv_obj_set_style_opa(r, LV_OPA_50, 0);

        char buf[96];
        cJSON *jnm = cJSON_GetObjectItem(e, "name");
        ascii_fold(cJSON_IsString(jnm) ? jnm->valuestring : jid->valuestring, buf, sizeof buf);
        cell(r, buf, W_NAME, &lv_font_montserrat_20, vc, false);
        fmt_temp(cJSON_GetObjectItem(e, "temperature_c"), buf, sizeof buf);
        cell(r, buf, W_TEMP, &lv_font_montserrat_20, vc, true);
        cJSON *jh = cJSON_GetObjectItem(e, "humidity_pct");
        if (cJSON_IsNumber(jh)) snprintf(buf, sizeof buf, "%d%%", jh->valueint); else snprintf(buf, sizeof buf, "-");
        cell(r, buf, W_HUM, &lv_font_montserrat_20, vc, true);
        fmt_temp(cJSON_GetObjectItem(e, "dewpoint_c"), buf, sizeof buf);
        cell(r, buf, W_DEW, &lv_font_montserrat_20, vc, true);
        cJSON *jb = cJSON_GetObjectItem(e, "air_band");
        cJSON *jbl = cJSON_GetObjectItem(e, "air_band_label");
        if (cJSON_IsNumber(jb) && cJSON_IsString(jbl)) {
            ascii_fold(jbl->valuestring, buf, sizeof buf);
            cell(r, buf, W_AIR, &lv_font_montserrat_20, stale ? C_DIM : aq_band_color(jb->valueint), true);
        } else {
            cell(r, "-", W_AIR, &lv_font_montserrat_20, C_DIM, true);
        }

        // notable tail: "Radon 48Bq  Pressure 1010hPa" (server-authored labels/units)
        char note[160] = "";
        int n = 0;
        cJSON *jn = cJSON_GetObjectItem(e, "notable"), *it;
        cJSON_ArrayForEach(it, jn) {
            cJSON *lb = cJSON_GetObjectItem(it, "label"), *v = cJSON_GetObjectItem(it, "value");
            cJSON *u = cJSON_GetObjectItem(it, "unit");
            if (!cJSON_IsString(lb) || !cJSON_IsNumber(v) || n >= (int)sizeof note - 40) continue;
            char lf[24], uf[12];
            ascii_fold(lb->valuestring, lf, sizeof lf);
            ascii_fold(cJSON_IsString(u) ? u->valuestring : "", uf, sizeof uf);
            n += snprintf(note + n, sizeof note - n, "%s%s %g%s", n ? "  " : "", lf, v->valuedouble, uf);
        }
        cell(r, note, 0, &lv_font_montserrat_14, vc, false);

        snprintf(s_row_id[nrow], ID_LEN, "%s", jid->valuestring);
        button(r, hidden ? LV_SYMBOL_EYE_OPEN : LV_SYMBOL_EYE_CLOSE, W_BTN, on_hide_toggle, s_row_id[nrow]);
        nrow++;
    }

    lv_obj_update_layout(parent);
    lv_obj_scroll_to_y(parent, y, LV_ANIM_OFF);                 // keep the place across the 10 s refresh
}
