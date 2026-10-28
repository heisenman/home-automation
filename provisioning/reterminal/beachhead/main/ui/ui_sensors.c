// Sensors view (see ui_sensors.h). Rebuilt on every refresh (10 s) from /api/v1/panel/sensors; the
// scroll position is carried across the rebuild. Two modes: the room grid (default) and the per-sensor
// Edit list where sensors are hidden / un-hidden. Fonts compiled: montserrat 14/20/28 only.
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
// (the room grid uses the same columns, with the button's width going to the notable column)
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
static bool s_edit;                 // Edit mode: per-sensor list, hidden rows shown dimmed, hide/un-hide buttons
static void (*s_changed)(void);

// per-row button payloads (device ids) — stable storage for the click user_data across one render
#define MAX_ROWS 96
static char s_row_id[MAX_ROWS][ID_LEN];

#define MAX_PER_ROOM 12             // sensors stacked in one room row (more are dropped from the grid)

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

static void on_edit(lv_event_t *e)
{
    (void)e;
    s_edit = !s_edit;
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

// ── room grid: one row per room, a sensor's values stacked one above the other ──
// A column cell is a vertical box with one label per sensor that HAS that value, so two thermometers in
// a room read as two temperatures, one above the other, while a room with one shows a single line.
static lv_obj_t *col_box(lv_obj_t *row, int w)
{
    lv_obj_t *c = lv_obj_create(row);
    lv_obj_set_height(c, LV_SIZE_CONTENT);
    if (w > 0) lv_obj_set_width(c, w); else { lv_obj_set_width(c, 1); lv_obj_set_flex_grow(c, 1); }
    lv_obj_set_style_bg_opa(c, 0, 0);
    lv_obj_set_style_border_width(c, 0, 0);
    lv_obj_set_style_pad_all(c, 0, 0);
    lv_obj_set_style_pad_row(c, 2, 0);
    lv_obj_set_flex_flow(c, LV_FLEX_FLOW_COLUMN);
    lv_obj_clear_flag(c, LV_OBJ_FLAG_SCROLLABLE);
    return c;
}

static void line(lv_obj_t *col, const char *txt, const lv_font_t *font, uint32_t color, bool right)
{
    lv_obj_t *l = lv_label_create(col);
    lv_label_set_text(l, txt);
    lv_obj_set_width(l, lv_pct(100));
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, lv_color_hex(color), 0);
    lv_label_set_long_mode(l, LV_LABEL_LONG_DOT);
    if (right) lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_RIGHT, 0);
}

// notable tail: "Radon 48Bq  Pressure 1010hPa" (server-authored labels/units); "" when the sensor has none
static void fmt_notable(cJSON *e, char *note, size_t cap)
{
    int n = 0;
    note[0] = 0;
    cJSON *jn = cJSON_GetObjectItem(e, "notable"), *it;
    cJSON_ArrayForEach(it, jn) {
        cJSON *lb = cJSON_GetObjectItem(it, "label"), *v = cJSON_GetObjectItem(it, "value");
        cJSON *u = cJSON_GetObjectItem(it, "unit");
        if (!cJSON_IsString(lb) || !cJSON_IsNumber(v) || n >= (int)cap - 40) continue;
        char lf[24], uf[12];
        ascii_fold(lb->valuestring, lf, sizeof lf);
        ascii_fold(cJSON_IsString(u) ? u->valuestring : "", uf, sizeof uf);
        n += snprintf(note + n, cap - n, "%s%s %g%s", n ? "  " : "", lf, v->valuedouble, uf);
    }
}

static bool is_stale(cJSON *e)
{
    cJSON *jage = cJSON_GetObjectItem(e, "age_s");
    return cJSON_IsNumber(jage) && jage->valuedouble > STALE_S;
}

// one room's row from its (visible) sensors, in server order
static void room_row(lv_obj_t *parent, const char *room_name, cJSON **ss, int n)
{
    lv_obj_t *r = row_box(parent, true);
    lv_obj_set_flex_align(r, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);  // top-align stacks
    char buf[160];
    ascii_fold(room_name, buf, sizeof buf);
    line(col_box(r, W_NAME), buf, &lv_font_montserrat_20, C_TEXT, false);

    lv_obj_t *ct = col_box(r, W_TEMP), *ch = col_box(r, W_HUM), *cd = col_box(r, W_DEW);
    lv_obj_t *ca = col_box(r, W_AIR), *cn = col_box(r, 0);
    int kt = 0, kh = 0, kd = 0, ka = 0;
    for (int i = 0; i < n; i++) {
        cJSON *e = ss[i], *j;
        uint32_t vc = is_stale(e) ? C_DIM : C_TEXT;
        if (cJSON_IsNumber(j = cJSON_GetObjectItem(e, "temperature_c"))) {
            fmt_temp(j, buf, sizeof buf); line(ct, buf, &lv_font_montserrat_20, vc, true); kt++;
        }
        if (cJSON_IsNumber(j = cJSON_GetObjectItem(e, "humidity_pct"))) {
            snprintf(buf, sizeof buf, "%d%%", j->valueint); line(ch, buf, &lv_font_montserrat_20, vc, true); kh++;
        }
        if (cJSON_IsNumber(j = cJSON_GetObjectItem(e, "dewpoint_c"))) {
            fmt_temp(j, buf, sizeof buf); line(cd, buf, &lv_font_montserrat_20, vc, true); kd++;
        }
        cJSON *jb = cJSON_GetObjectItem(e, "air_band"), *jbl = cJSON_GetObjectItem(e, "air_band_label");
        if (cJSON_IsNumber(jb) && cJSON_IsString(jbl)) {
            ascii_fold(jbl->valuestring, buf, sizeof buf);
            line(ca, buf, &lv_font_montserrat_20, vc == C_DIM ? C_DIM : aq_band_color(jb->valueint), true); ka++;
        }
        fmt_notable(e, buf, sizeof buf);
        if (buf[0]) line(cn, buf, &lv_font_montserrat_14, vc, false);
    }
    if (!kt) line(ct, "-", &lv_font_montserrat_20, C_DIM, true);
    if (!kh) line(ch, "-", &lv_font_montserrat_20, C_DIM, true);
    if (!kd) line(cd, "-", &lv_font_montserrat_20, C_DIM, true);
    if (!ka) line(ca, "-", &lv_font_montserrat_20, C_DIM, true);
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

    // count the hidden ones that still exist (a hidden id whose sensor is gone doesn't count), and the rooms
    int total = 0, nhid = 0, nroom = 0;
    char room[ID_LEN] = "";
    cJSON *e;
    cJSON_ArrayForEach(e, rows) {
        cJSON *jid = cJSON_GetObjectItem(e, "device_id");
        if (!cJSON_IsString(jid)) continue;
        total++;
        if (find_hidden(jid->valuestring) >= 0) { nhid++; continue; }
        cJSON *jroom = cJSON_GetObjectItem(e, "room");
        if (cJSON_IsString(jroom) && strcmp(room, jroom->valuestring) != 0) {
            snprintf(room, sizeof room, "%s", jroom->valuestring); nroom++;
        }
    }

    // toolbar: grid -> [ n rooms - m sensors (k hidden) ] ... [ Edit ];  edit -> [ tap to hide ] ... [ Done ]
    lv_obj_t *bar = row_box(parent, false);
    char t[80];
    if (s_edit) snprintf(t, sizeof t, "Tap " LV_SYMBOL_EYE_CLOSE " to hide a sensor, " LV_SYMBOL_EYE_OPEN " to show it");
    else if (nhid) snprintf(t, sizeof t, "%d rooms  -  %d sensors (%d hidden)", nroom, total - nhid, nhid);
    else snprintf(t, sizeof t, "%d rooms  -  %d sensors", nroom, total);
    cell(bar, t, 0, &lv_font_montserrat_20, C_DIM, false);
    button(bar, s_edit ? LV_SYMBOL_OK "  Done" : LV_SYMBOL_EDIT "  Edit", 160, on_edit, NULL);

    // column header
    lv_obj_t *hdr = row_box(parent, false);
    cell(hdr, s_edit ? "Sensor" : "Room", W_NAME, &lv_font_montserrat_14, C_HEAD, false);
    cell(hdr, "Temp", W_TEMP, &lv_font_montserrat_14, C_HEAD, true);
    cell(hdr, "Hum", W_HUM, &lv_font_montserrat_14, C_HEAD, true);
    cell(hdr, "Dew", W_DEW, &lv_font_montserrat_14, C_HEAD, true);
    cell(hdr, "Air", W_AIR, &lv_font_montserrat_14, C_HEAD, true);
    cell(hdr, "Other", 0, &lv_font_montserrat_14, C_HEAD, false);
    if (s_edit) { lv_obj_t *pad = lv_label_create(hdr); lv_label_set_text(pad, ""); lv_obj_set_width(pad, W_BTN); }

    if (!s_edit) {
        // rows arrive grouped by room (server order), so a room is a run of consecutive rows
        cJSON *ss[MAX_PER_ROOM];
        int n = 0;
        const char *rname = NULL;
        room[0] = 0;
        cJSON_ArrayForEach(e, rows) {
            cJSON *jid = cJSON_GetObjectItem(e, "device_id");
            if (!cJSON_IsString(jid) || find_hidden(jid->valuestring) >= 0) continue;
            cJSON *jroom = cJSON_GetObjectItem(e, "room");
            const char *rm = cJSON_IsString(jroom) ? jroom->valuestring : "";
            if (n && strcmp(room, rm) != 0) { room_row(parent, rname, ss, n); n = 0; }
            if (!n) {
                snprintf(room, sizeof room, "%s", rm);
                cJSON *jrn = cJSON_GetObjectItem(e, "room_name");
                rname = cJSON_IsString(jrn) ? jrn->valuestring : rm;
            }
            if (n < MAX_PER_ROOM) ss[n++] = e;
        }
        if (n) room_row(parent, rname, ss, n);
        lv_obj_update_layout(parent);
        lv_obj_scroll_to_y(parent, y, LV_ANIM_OFF);             // keep the place across the 10 s refresh
        return;
    }

    room[0] = 0;
    int nrow = 0;
    cJSON_ArrayForEach(e, rows) {
        cJSON *jid = cJSON_GetObjectItem(e, "device_id");
        if (!cJSON_IsString(jid) || nrow >= MAX_ROWS) continue;
        bool hidden = find_hidden(jid->valuestring) >= 0;

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

        uint32_t vc = is_stale(e) ? C_DIM : C_TEXT;
        lv_obj_t *r = row_box(parent, true);
        if (hidden) lv_obj_set_style_opa(r, LV_OPA_50, 0);

        char buf[160];
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
            cell(r, buf, W_AIR, &lv_font_montserrat_20, vc == C_DIM ? C_DIM : aq_band_color(jb->valueint), true);
        } else {
            cell(r, "-", W_AIR, &lv_font_montserrat_20, C_DIM, true);
        }
        fmt_notable(e, buf, sizeof buf);
        cell(r, buf, 0, &lv_font_montserrat_14, vc, false);

        snprintf(s_row_id[nrow], ID_LEN, "%s", jid->valuestring);
        button(r, hidden ? LV_SYMBOL_EYE_OPEN : LV_SYMBOL_EYE_CLOSE, W_BTN, on_hide_toggle, s_row_id[nrow]);
        nrow++;
    }

    lv_obj_update_layout(parent);
    lv_obj_scroll_to_y(parent, y, LV_ANIM_OFF);                 // keep the place across the 10 s refresh
}
